"""What a line costs the customer, and how that splits into tax.

The server is the sole authority on price and discount (CLAUDE.md rule 8):
the client sends a medicine, a quantity and a requested discount, and every
figure below is recomputed here. A client that asks for a cheaper price than
policy allows is refused, not obeyed.

Retail MRP in Indian pharmacy is **GST-inclusive** — the printed price is
what the customer pays, and the tax is carved out of it. That is the
opposite of the purchase side, where a distributor bills tax on top. The
store can flip this with `settings.price_includes_gst`.

    line_gross    = qty_units × unit_mrp
    discount_amt  = round(line_gross × discount_pct/100, 2)
    line_net      = line_gross − discount_amt
    taxable_value = round(line_net / (1 + gst_rate/100), 2)     # inclusive
    gst_amount    = line_net − taxable_value
        intra-state → cgst = sgst = gst_amount / 2
        inter-state → igst = gst_amount
"""

from decimal import Decimal

from app.core.exceptions import AppError
from app.db.types import quantize_money
from app.models.inventory import Batch
from app.models.store import StoreSettings

ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def quantize_rate(value: Decimal) -> Decimal:
    """Percentages carry two decimals, matching the Rate() columns."""
    return Decimal(value).quantize(Decimal("0.01"))


class DiscountError(AppError):
    code = "DISCOUNT_LIMIT_EXCEEDED"


class LinePrice:
    """Every figure a printed invoice line needs."""

    __slots__ = (
        "qty_units", "unit_mrp", "gross_amount", "discount_pct", "discount_amount",
        "net_amount", "gst_rate", "taxable_value", "gst_amount",
        "cgst_amount", "sgst_amount", "igst_amount",
    )

    def __init__(self, **kw) -> None:
        for key in self.__slots__:
            setattr(self, key, kw.get(key, ZERO))

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__slots__}


def split_tax(gst_amount: Decimal, is_intra_state: bool) -> tuple[Decimal, Decimal, Decimal]:
    """(cgst, sgst, igst). Odd paise go to CGST so the halves always add back."""
    if not is_intra_state:
        return ZERO, ZERO, quantize_money(gst_amount)
    sgst = quantize_money(gst_amount / 2)
    cgst = quantize_money(gst_amount - sgst)
    return cgst, sgst, ZERO


def price_line(
    *,
    qty_units: Decimal,
    unit_mrp: Decimal,
    gst_rate: Decimal,
    discount_pct: Decimal,
    is_intra_state: bool,
    price_includes_gst: bool = True,
) -> LinePrice:
    """Price one line. No policy checks here — see `resolve_discount`."""
    gross = quantize_money(Decimal(qty_units) * Decimal(unit_mrp))
    discount_amount = quantize_money(gross * Decimal(discount_pct) / HUNDRED)
    net = quantize_money(gross - discount_amount)

    rate = Decimal(gst_rate or 0)
    if price_includes_gst:
        # Carve the tax out of the price the customer pays.
        taxable = quantize_money(net / (1 + rate / HUNDRED))
        gst_amount = quantize_money(net - taxable)
    else:
        taxable = net
        gst_amount = quantize_money(net * rate / HUNDRED)
        net = quantize_money(taxable + gst_amount)

    cgst, sgst, igst = split_tax(gst_amount, is_intra_state)
    return LinePrice(
        qty_units=Decimal(qty_units),
        unit_mrp=Decimal(unit_mrp),
        gross_amount=gross,
        discount_pct=Decimal(discount_pct),
        discount_amount=discount_amount,
        net_amount=net,
        gst_rate=rate,
        taxable_value=taxable,
        gst_amount=gst_amount,
        cgst_amount=cgst,
        sgst_amount=sgst,
        igst_amount=igst,
    )


def resolve_discount(
    *,
    requested: Decimal | None,
    batch: Batch,
    settings: StoreSettings,
    is_admin: bool = False,
    override_reason: str | None = None,
    label: str = "this line",
    field: str = "discount_pct",
) -> Decimal:
    """The discount actually allowed on a line.

    Resolution order is batch → store, for both the suggestion and the
    ceiling (SPEC §3.3). A batch near its expiry is often cleared at a
    deeper discount than fresh stock, which is exactly why the limits sit on
    the consignment rather than the medicine.

    An admin may go past the ceiling — up to 100 % — but only with a reason,
    which is recorded on the sale and in the audit log. A sales executive
    cannot exceed it by any route.
    """
    ceiling = settings.max_discount_pct
    suggested = settings.default_discount_pct
    if batch.max_discount_pct is not None:
        ceiling = batch.max_discount_pct
    if batch.default_discount_pct is not None:
        suggested = batch.default_discount_pct
    suggested = min(suggested, ceiling)

    if requested is None:
        return suggested

    # Same two decimals as the stored rate columns, so "35" and "35.00"
    # cannot come back as two different percentages.
    wanted = quantize_rate(requested)
    if wanted < ZERO:
        raise DiscountError("A discount cannot be negative.", field=field)
    if wanted > HUNDRED:
        raise DiscountError("A discount cannot exceed 100 %.", field=field)

    if wanted > ceiling:
        if is_admin and override_reason:
            return wanted
        if is_admin:
            raise DiscountError(
                f"{wanted}% is above the {ceiling}% limit for {label}. "
                f"An override needs a reason.",
                code="DISCOUNT_OVERRIDE_REASON_REQUIRED",
                field=field,
            )
        raise DiscountError(
            f"{wanted}% is above the {ceiling}% limit for {label}. "
            f"Ask an administrator to approve a deeper discount.",
            field=field,
        )
    return wanted


class InvoiceTotals:
    """The totals card, and what actually gets paid."""

    __slots__ = (
        "gross_amount", "discount_amount", "taxable_amount",
        "cgst_amount", "sgst_amount", "igst_amount",
        "gst_amount", "sub_total", "round_off", "net_amount",
    )

    def __init__(self, **kw) -> None:
        for key in self.__slots__:
            setattr(self, key, kw.get(key, ZERO))

    def as_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__slots__}


def total_invoice(lines: list[LinePrice], settings: StoreSettings) -> InvoiceTotals:
    """Add the lines up and round the payable to the nearest rupee.

    Rounding happens **once**, on the invoice total — never per line — so the
    tax break-up still reconciles with the sum of the lines.
    """
    gross = sum((line.gross_amount for line in lines), ZERO)
    discount = sum((line.discount_amount for line in lines), ZERO)
    taxable = sum((line.taxable_value for line in lines), ZERO)
    cgst = sum((line.cgst_amount for line in lines), ZERO)
    sgst = sum((line.sgst_amount for line in lines), ZERO)
    igst = sum((line.igst_amount for line in lines), ZERO)
    sub_total = sum((line.net_amount for line in lines), ZERO)

    sub_total = quantize_money(sub_total)
    if settings.round_off_enabled:
        payable = sub_total.quantize(Decimal("1"))
        round_off = quantize_money(payable - sub_total)
    else:
        payable = sub_total
        round_off = ZERO

    return InvoiceTotals(
        gross_amount=quantize_money(gross),
        discount_amount=quantize_money(discount),
        taxable_amount=quantize_money(taxable),
        cgst_amount=quantize_money(cgst),
        sgst_amount=quantize_money(sgst),
        igst_amount=quantize_money(igst),
        gst_amount=quantize_money(cgst + sgst + igst),
        sub_total=sub_total,
        round_off=round_off,
        net_amount=quantize_money(payable),
    )
