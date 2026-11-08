"""Goods inward: turning a supplier's invoice into stock.

Posting one purchase does five things, all in a single transaction:

1. computes each line's tax the way a distributor's invoice does — cost is
   **GST-exclusive** here, unlike retail MRP which is GST-inclusive;
2. creates (or tops up) one batch per line, snapshotting pack, MRP, GST rate
   and landed cost;
3. writes a `PURCHASE` row to the stock ledger for every line, so the ledger
   still explains every unit on the shelf;
4. resets each medicine's `stock_baseline_units`, which is what the order
   book measures its 60 % decline against (Phase 6);
5. reserves a gap-free internal document number.

Cancelling reverses (3) and (4) through fresh ledger rows rather than
deleting anything, and is refused once a single unit has been sold.
"""

from uuid import UUID

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.db.types import quantize_money, quantize_qty
from app.models.catalog import Medicine
from app.models.enums import (
    DocumentType,
    GstRegistrationType,
    MovementType,
    PurchasePaymentStatus,
    ReferenceType,
)
from app.models.inventory import Batch, StockLedger
from app.models.party import Supplier
from app.models.purchase import PurchaseInvoice, PurchaseItem
from app.models.store import Store, StoreSettings
from app.services import inventory, sequences
from app.utils.clock import business_date

ZERO = Decimal("0.00")
ZERO_QTY = Decimal("0.000")
HUNDRED = Decimal("100")

# A sale or a return against a batch means a financial document depends on
# the stock this purchase brought in; the purchase can no longer be undone.
PROTECTED_MOVEMENTS = (
    MovementType.SALE,
    MovementType.SALE_RETURN,
    MovementType.PURCHASE_RETURN,
    MovementType.VOID_REVERSAL,
)


class PurchaseError(AppError):
    code = "PURCHASE_INVALID"


class LineTax:
    """The tax break-up of one purchase line."""

    __slots__ = (
        "gross", "discount_amount", "taxable_value",
        "cgst", "sgst", "igst", "gst_amount", "line_total",
    )

    def __init__(self, **kw) -> None:
        for key in self.__slots__:
            setattr(self, key, kw.get(key, ZERO))


def compute_line_tax(
    *,
    qty_packs: Decimal,
    cost_per_pack: Decimal,
    discount_pct: Decimal,
    gst_rate: Decimal,
    is_intra_state: bool,
) -> LineTax:
    """Distributor arithmetic: rate × qty, less trade discount, plus GST.

    Free goods never appear here — they are received into stock but not
    billed, so they carry no taxable value.
    """
    gross = quantize_money(Decimal(qty_packs) * Decimal(cost_per_pack))
    discount_amount = quantize_money(gross * Decimal(discount_pct) / HUNDRED)
    taxable = quantize_money(gross - discount_amount)
    gst_amount = quantize_money(taxable * Decimal(gst_rate) / HUNDRED)

    if is_intra_state:
        # Half each, with the remaining paise put on CGST so the two always
        # add back to the total tax.
        sgst = quantize_money(gst_amount / 2)
        cgst = quantize_money(gst_amount - sgst)
        igst = ZERO
    else:
        cgst = sgst = ZERO
        igst = gst_amount

    return LineTax(
        gross=gross,
        discount_amount=discount_amount,
        taxable_value=taxable,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        gst_amount=gst_amount,
        line_total=quantize_money(taxable + gst_amount),
    )


def landed_cost_per_pack(
    *,
    taxable_value: Decimal,
    gst_amount: Decimal,
    total_packs: Decimal,
    settings: StoreSettings,
) -> Decimal:
    """What one pack of this consignment actually cost the shop.

    Two adjustments the invoice rate does not make:

    * free goods are spread over the whole quantity — 10 + 2 free at ₹100
      is a landed ₹83.33, and pretending it is ₹100 overstates cost and
      understates margin on every later report;
    * input GST is only a cost when it cannot be claimed back. A regular
      registered dealer credits it, so cost is net of tax; a composition or
      unregistered dealer pays it for good, so it is part of the cost.
    """
    if total_packs <= 0:
        return ZERO
    base = Decimal(taxable_value)
    if settings.gst_registration_type != GstRegistrationType.REGULAR.value:
        base += Decimal(gst_amount)
    return quantize_money(base / Decimal(total_packs))


def resolve_intra_state(
    store: Store | None, supplier: Supplier, override: bool | None = None
) -> bool:
    """CGST + SGST when both parties sit in one state, otherwise IGST.

    Falls back to intra-state when either code is unknown: that is the
    common case for a single-location shop, and the operator can override.
    """
    if override is not None:
        return override
    store_code = (store.state_code or "").strip() if store else ""
    supplier_code = (supplier.state_code or "").strip()
    if not store_code or not supplier_code:
        return True
    return store_code == supplier_code


def _existing_batch(db: Session, store_id: UUID, medicine_id: UUID, batch_no: str, expiry: date):
    return (
        db.query(Batch)
        .filter(
            Batch.store_id == store_id,
            Batch.medicine_id == medicine_id,
            Batch.batch_no == batch_no,
            Batch.expiry_date == expiry,
        )
        .one_or_none()
    )


def post_purchase(
    db: Session,
    *,
    store_id: UUID,
    supplier: Supplier,
    payload,
    settings: StoreSettings,
    actor_id: UUID,
) -> tuple[PurchaseInvoice, list[str]]:
    """Receive a supplier invoice into stock. Returns the invoice and any
    warnings worth showing the operator (nothing here is fatal)."""
    if not payload.items:
        raise PurchaseError("A purchase must have at least one line.", field="items")

    duplicate = (
        db.query(func.count(PurchaseInvoice.id))
        .filter(
            PurchaseInvoice.store_id == store_id,
            PurchaseInvoice.supplier_id == supplier.id,
            PurchaseInvoice.supplier_invoice_no == payload.supplier_invoice_no.strip(),
        )
        .scalar()
    )
    if duplicate:
        raise ConflictError(
            f"Invoice {payload.supplier_invoice_no} from {supplier.name} has already been "
            f"received. Receiving it twice would double the stock.",
            code="PURCHASE_EXISTS",
            field="supplier_invoice_no",
        )

    store = db.get(Store, store_id)
    is_intra_state = resolve_intra_state(store, supplier, payload.is_intra_state)
    received = payload.received_date or business_date()

    invoice = PurchaseInvoice(
        store_id=store_id,
        supplier_id=supplier.id,
        supplier_invoice_no=payload.supplier_invoice_no.strip(),
        supplier_invoice_date=payload.supplier_invoice_date,
        doc_number=sequences.next_document_number(db, store_id, DocumentType.PURCHASE),
        received_date=received,
        is_intra_state=is_intra_state,
        other_charges=payload.other_charges or ZERO,
        payment_status=str(payload.payment_status or PurchasePaymentStatus.UNPAID),
        notes=payload.notes,
        created_by_id=actor_id,
        updated_by_id=actor_id,
    )
    db.add(invoice)
    db.flush()

    warnings = _post_lines(
        db,
        invoice=invoice,
        lines=payload.items,
        supplier=supplier,
        settings=settings,
        actor_id=actor_id,
        is_intra_state=is_intra_state,
        received=received,
    )

    _apply_totals(invoice, _totals_of(invoice), settings)
    db.flush()
    return invoice, warnings


def _totals_of(invoice: PurchaseInvoice) -> dict:
    """Sum the invoice from its lines.

    Recomputed from what is on the document rather than accumulated as lines
    are added, so appending to an invoice later arrives at the same figures a
    single posting would have — there is one definition of the total.
    """
    return {
        "taxable": sum((i.taxable_value for i in invoice.items), ZERO),
        "discount": sum((i.discount_amount for i in invoice.items), ZERO),
        "cgst": sum((i.cgst_amount for i in invoice.items), ZERO),
        "sgst": sum((i.sgst_amount for i in invoice.items), ZERO),
        "igst": sum((i.igst_amount for i in invoice.items), ZERO),
    }


def _post_lines(
    db: Session,
    *,
    invoice: PurchaseInvoice,
    lines,
    supplier: Supplier,
    settings: StoreSettings,
    actor_id: UUID,
    is_intra_state: bool,
    received: date,
) -> list[str]:
    """Turn invoice lines into stock: a PurchaseItem, a batch, a ledger row.

    Shared by the first posting and by any line appended afterwards, so a
    line added on Tuesday is received exactly as one keyed on Monday was.
    """
    store_id = invoice.store_id
    warnings: list[str] = []

    for index, line in enumerate(lines):
        medicine = db.get(Medicine, line.medicine_id)
        if medicine is None or medicine.store_id != store_id:
            raise NotFoundError(
                f"Line {index + 1}: medicine not found.", field=f"items.{index}.medicine_id"
            )

        units_per_pack = medicine.units_per_pack or 1
        free_packs = line.free_qty_packs or ZERO_QTY
        total_packs = quantize_qty(Decimal(line.qty_packs) + Decimal(free_packs))
        qty_units = quantize_qty(total_packs * Decimal(units_per_pack))

        # A medicine's batch is a traceability record and stays mandatory;
        # a general item is allowed to arrive with none, and gets a
        # generated placeholder instead (services.inventory.resolve_batch_no).
        resolved_batch_no = inventory.resolve_batch_no(line.batch_no, medicine=medicine)

        # One shelf date, however the carton stated it. Doing this before the
        # PurchaseItem is built means the invoice line, the batch and the
        # duplicate-lot lookup all agree on the same date.
        (
            expiry_date,
            expiry_basis,
            mfg_date,
            best_before_months,
        ) = inventory.resolve_expiry(
            basis=line.expiry_basis,
            expiry_date=line.expiry_date,
            mfg_date=line.mfg_date,
            best_before_months=line.best_before_months,
        )

        tax = compute_line_tax(
            qty_packs=line.qty_packs,
            cost_per_pack=line.cost_per_pack,
            discount_pct=line.discount_pct or ZERO,
            gst_rate=line.gst_rate,
            is_intra_state=is_intra_state,
        )
        cost = landed_cost_per_pack(
            taxable_value=tax.taxable_value,
            gst_amount=tax.gst_amount,
            total_packs=total_packs,
            settings=settings,
        )

        item = PurchaseItem(
            purchase_invoice_id=invoice.id,
            medicine_id=medicine.id,
            batch_no=resolved_batch_no,
            expiry_date=expiry_date,
            units_per_pack=units_per_pack,
            qty_packs=quantize_qty(line.qty_packs),
            free_qty_packs=quantize_qty(free_packs),
            qty_units=qty_units,
            cost_per_pack=quantize_money(line.cost_per_pack),
            mrp_per_pack=quantize_money(line.mrp_per_pack),
            discount_pct=line.discount_pct or ZERO,
            gst_rate=line.gst_rate,
            taxable_value=tax.taxable_value,
            discount_amount=tax.discount_amount,
            cgst_amount=tax.cgst,
            sgst_amount=tax.sgst,
            igst_amount=tax.igst,
            line_total=tax.line_total,
        )
        db.add(item)
        db.flush()

        batch = _existing_batch(db, store_id, medicine.id, item.batch_no, expiry_date)
        if batch is None:
            batch = inventory.create_batch(
                db,
                store_id=store_id,
                medicine=medicine,
                batch_no=item.batch_no,
                expiry_date=expiry_date,
                expiry_basis=expiry_basis,
                mfg_date=mfg_date,
                best_before_months=best_before_months,
                qty_units=qty_units,
                mrp_per_pack=item.mrp_per_pack,
                cost_per_pack=cost,
                gst_rate=line.gst_rate,
                default_discount_pct=line.default_discount_pct,
                max_discount_pct=line.max_discount_pct,
                received_date=received,
                movement_type=MovementType.PURCHASE,
                actor_id=actor_id,
                reference_type=ReferenceType.PURCHASE_ITEM,
                reference_id=item.id,
                reason=f"Goods inward {invoice.doc_number}",
            )
            batch.supplier_id = supplier.id
        else:
            # Same lot arriving again: top up the quantity and leave the
            # commercial terms as first recorded. One batch number is one
            # manufacturing lot with one printed MRP, so a different figure
            # on the new invoice is a discrepancy to flag, not to apply
            # retrospectively to stock already on the shelf.
            if batch.mrp_per_pack != item.mrp_per_pack:
                warnings.append(
                    f"{medicine.brand_name} batch {item.batch_no}: invoice MRP "
                    f"{item.mrp_per_pack} differs from the {batch.mrp_per_pack} already "
                    f"recorded for this batch. Stock was topped up at the existing MRP."
                )
            inventory.post_movement(
                db,
                batch=batch,
                movement_type=MovementType.PURCHASE,
                qty_units_delta=qty_units,
                actor_id=actor_id,
                reference_type=ReferenceType.PURCHASE_ITEM,
                reference_id=item.id,
                reason=f"Goods inward {invoice.doc_number}",
            )
            medicine.stock_baseline_units = quantize_qty(
                (medicine.stock_baseline_units or ZERO_QTY) + qty_units
            )

        item.batch_id = batch.id

        # Goods that never expire have a sentinel date, not a real one, so
        # only warn about stock that genuinely has a shelf life.
        if batch.has_expiry and expiry_date <= received:
            warnings.append(
                f"{medicine.brand_name} batch {item.batch_no} expires on "
                f"{expiry_date} — it cannot be sold."
            )


    return warnings


def _apply_totals(invoice: PurchaseInvoice, totals: dict, settings: StoreSettings) -> None:
    invoice.subtotal_taxable = quantize_money(totals["taxable"])
    invoice.discount_amount = quantize_money(totals["discount"])
    invoice.cgst_amount = quantize_money(totals["cgst"])
    invoice.sgst_amount = quantize_money(totals["sgst"])
    invoice.igst_amount = quantize_money(totals["igst"])

    payable = quantize_money(
        invoice.subtotal_taxable
        + invoice.cgst_amount
        + invoice.sgst_amount
        + invoice.igst_amount
        + (invoice.other_charges or ZERO)
    )
    if settings.round_off_enabled:
        rounded = payable.quantize(Decimal("1"))
        invoice.round_off = quantize_money(rounded - payable)
        invoice.total_amount = quantize_money(rounded)
    else:
        invoice.round_off = ZERO
        invoice.total_amount = payable


def append_lines(
    db: Session,
    *,
    invoice: PurchaseInvoice,
    lines,
    settings: StoreSettings,
    actor_id: UUID,
) -> list[str]:
    """Add lines to an invoice already received.

    Goods from one supplier invoice do not always reach the shelf in one
    sitting: a carton is opened the next morning, or a line was missed on the
    way in. Making the operator cancel and re-key the whole document to add
    one item costs more than it protects — cancelling reverses stock that has
    already been sold from.

    This is a deliberate departure from "a posted purchase is not editable",
    and it is bounded: lines can only be **added**. Nothing existing is
    edited or removed, the invoice keeps its number and its identity, the
    totals are recomputed from the lines rather than adjusted by hand, and
    the caller writes an audit row with the before and after. Removing a line
    is still cancel-and-re-enter, because that is the case where stock may
    already have gone out of the door.
    """
    if not lines:
        raise PurchaseError("Nothing to add.", field="items")
    if invoice.is_cancelled:
        raise ConflictError(
            f"Purchase {invoice.doc_number} is cancelled. Its stock has already been "
            f"reversed, so adding to it would put goods back on the shelf under a "
            f"document that says none arrived.",
            code="PURCHASE_CANCELLED",
        )

    supplier = db.get(Supplier, invoice.supplier_id)
    if supplier is None:
        raise NotFoundError("Supplier not found.", field="supplier_id")

    warnings = _post_lines(
        db,
        invoice=invoice,
        lines=lines,
        supplier=supplier,
        settings=settings,
        actor_id=actor_id,
        # Both settled when the invoice was received; a later line is part of
        # the same document and inherits them rather than being re-derived
        # from today's date or a re-read of the supplier's state.
        is_intra_state=invoice.is_intra_state,
        received=invoice.received_date,
    )

    invoice.updated_by_id = actor_id
    db.flush()
    # The caller loaded the invoice with its lines, so the collection in
    # memory predates the ones just added. Summing it as-is would leave the
    # document's total describing only the lines it arrived with.
    db.expire(invoice, ["items"])
    _apply_totals(invoice, _totals_of(invoice), settings)
    db.flush()
    return warnings


def cancel_purchase(
    db: Session, *, invoice: PurchaseInvoice, reason: str, actor_id: UUID
) -> PurchaseInvoice:
    """Undo a purchase entered in error.

    The invoice row and its lines stay exactly as they were; the stock is
    given back through reversing ledger entries. Refused outright once
    anything has been sold from the batches it created, because the sale
    would then reference stock that never existed.
    """
    if invoice.is_cancelled:
        raise ConflictError(
            f"Purchase {invoice.doc_number} is already cancelled.",
            code="PURCHASE_CANCELLED",
        )

    batch_ids = [item.batch_id for item in invoice.items if item.batch_id]
    if batch_ids:
        sold = (
            db.query(func.count(StockLedger.id))
            .filter(
                StockLedger.batch_id.in_(batch_ids),
                StockLedger.movement_type.in_([m.value for m in PROTECTED_MOVEMENTS]),
            )
            .scalar()
        )
        if sold:
            raise ConflictError(
                f"Stock from purchase {invoice.doc_number} has already been sold or "
                f"returned ({sold} movement(s)). Raise a purchase return instead — "
                f"cancelling would leave those documents referring to stock that "
                f"never arrived.",
                code="PURCHASE_HAS_SALES",
            )

    for item in invoice.items:
        if not item.batch_id:
            continue
        batch = db.get(Batch, item.batch_id)
        if batch is None:
            continue
        if batch.qty_available_units < item.qty_units:
            raise ConflictError(
                f"Batch {batch.batch_no} holds {batch.qty_available_units} unit(s) but this "
                f"purchase brought in {item.qty_units}. Adjust the batch to match the shelf "
                f"before cancelling.",
                code="PURCHASE_STOCK_SHORT",
            )

        inventory.post_movement(
            db,
            batch=batch,
            movement_type=MovementType.PURCHASE_RETURN,
            qty_units_delta=-item.qty_units,
            actor_id=actor_id,
            reference_type=ReferenceType.PURCHASE_ITEM,
            reference_id=item.id,
            reason=f"Purchase {invoice.doc_number} cancelled: {reason}",
        )
        # post_movement only grows qty_received; a cancellation must shrink it
        # too, or the batch would claim to have received stock it never kept.
        batch.qty_received_units = quantize_qty(batch.qty_received_units - item.qty_units)
        if batch.qty_available_units <= ZERO_QTY:
            batch.is_blocked = True
            batch.block_reason = f"Purchase {invoice.doc_number} cancelled"

        medicine = db.get(Medicine, item.medicine_id)
        if medicine is not None:
            medicine.stock_baseline_units = max(
                ZERO_QTY,
                quantize_qty((medicine.stock_baseline_units or ZERO_QTY) - item.qty_units),
            )

    invoice.is_cancelled = True
    invoice.cancelled_at = datetime.now(timezone.utc)
    invoice.cancelled_by_id = actor_id
    invoice.cancel_reason = reason
    invoice.updated_by_id = actor_id
    db.flush()
    return invoice
