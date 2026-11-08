"""Billing: quoting a cart, and turning it into an invoice.

`quote` and `confirm` share one costing path deliberately. If the price
shown on screen and the price charged came from two pieces of code, they
would drift, and the customer would be handed a bill that does not match
what they were quoted.

`quote` writes nothing. `confirm` does everything in one transaction:
allocate batches under a row lock, decrement stock through the ledger,
reserve the invoice number from the locked sequence, and record the
payments. Any failure rolls the whole thing back — there is no state in
which stock has moved but no invoice exists.
"""

from uuid import UUID

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import AppError, NotFoundError
from app.db.types import quantize_money
from app.models.catalog import Medicine
from app.models.enums import (
    DocumentType,
    MovementType,
    PaymentMode,
    ReferenceType,
    SaleStatus,
)
from app.models.party import Customer, Doctor
from app.models.sale import Payment, Sale, SaleItem
from app.models.store import Store, StoreSettings
from app.models.user import User
from app.services import allocation, credit_notes, inventory, pricing, sequences
from app.utils.clock import business_date, to_business_date

ZERO = Decimal("0.00")


class SaleError(AppError):
    code = "SALE_INVALID"


class QuotedLine:
    """One priced slice of the cart: a medicine drawn from one batch."""

    __slots__ = (
        "medicine", "batch", "price", "line_no",
        "is_near_expiry", "requested_discount_pct",
    )

    def __init__(self, **kw) -> None:
        for key in self.__slots__:
            setattr(self, key, kw.get(key))


class Quote:
    __slots__ = ("lines", "totals", "warnings", "is_intra_state", "requires_doctor")

    def __init__(self, **kw) -> None:
        for key in self.__slots__:
            setattr(self, key, kw.get(key))


def resolve_intra_state(store: Store | None, customer: Customer | None) -> bool:
    """Retail is intra-state unless the customer's own state says otherwise.

    A walk-in has no state code, so the shop's own state applies — which is
    the right answer for over-the-counter sales.
    """
    if customer is None:
        return True
    store_code = (store.state_code or "").strip() if store else ""
    customer_code = (customer.state_code or "").strip()
    if not store_code or not customer_code:
        return True
    return store_code == customer_code


def build_quote(
    db: Session,
    *,
    store_id: UUID,
    items,
    settings: StoreSettings,
    customer: Customer | None = None,
    doctor_id: UUID | None = None,
    is_admin: bool = False,
    discount_override_reason: str | None = None,
) -> Quote:
    """Price a cart. Reads only — safe to call on every keystroke."""
    if not items:
        raise SaleError("The cart is empty.", field="items")

    store = db.get(Store, store_id)
    is_intra_state = resolve_intra_state(store, customer)
    today = business_date()
    near_expiry_cutoff = _add_days(today, settings.near_expiry_days)

    lines: list[QuotedLine] = []
    warnings: list[str] = []
    requires_doctor = False
    line_no = 0

    for index, item in enumerate(items):
        medicine = db.get(Medicine, item.medicine_id)
        if medicine is None or medicine.store_id != store_id:
            raise NotFoundError(
                f"Line {index + 1}: medicine not found.", field=f"items.{index}.medicine_id"
            )
        if not medicine.is_active:
            raise SaleError(
                f"{medicine.brand_name} has been withdrawn from the catalogue.",
                code="MEDICINE_INACTIVE",
                field=f"items.{index}.medicine_id",
            )

        if medicine.is_prescription_required:
            requires_doctor = True
            if doctor_id is None:
                raise SaleError(
                    f"{medicine.brand_name} is a {medicine.schedule_type} drug and needs a "
                    f"prescribing doctor on the invoice.",
                    code="PRESCRIPTION_REQUIRED",
                    field="doctor_id",
                )

        slices = allocation.allocate(
            db,
            store_id=store_id,
            medicine_id=medicine.id,
            qty_units=item.qty_units,
            medicine_name=medicine.brand_name,
            batch_id=getattr(item, "batch_id", None),
            field=f"items.{index}.qty_units",
        )

        for part in slices:
            batch = part.batch
            discount_pct = pricing.resolve_discount(
                requested=getattr(item, "discount_pct", None),
                batch=batch,
                settings=settings,
                is_admin=is_admin,
                override_reason=discount_override_reason,
                label=f"{medicine.brand_name} (batch {batch.batch_no})",
                field=f"items.{index}.discount_pct",
            )
            price = pricing.price_line(
                qty_units=part.qty_units,
                unit_mrp=batch.unit_mrp,
                gst_rate=batch.gst_rate,
                discount_pct=discount_pct,
                is_intra_state=is_intra_state,
                price_includes_gst=settings.price_includes_gst,
            )
            line_no += 1
            near = batch.expiry_date <= near_expiry_cutoff
            if near:
                warnings.append(
                    f"{medicine.brand_name} batch {batch.batch_no} expires on "
                    f"{batch.expiry_date} — tell the customer."
                )
            lines.append(
                QuotedLine(
                    medicine=medicine,
                    batch=batch,
                    price=price,
                    line_no=line_no,
                    is_near_expiry=near,
                    requested_discount_pct=getattr(item, "discount_pct", None),
                )
            )

        if len(slices) > 1:
            warnings.append(
                f"{medicine.brand_name} was drawn from {len(slices)} batches — the strips "
                f"handed over will carry different expiry dates."
            )

    totals = pricing.total_invoice([line.price for line in lines], settings)
    return Quote(
        lines=lines,
        totals=totals,
        warnings=warnings,
        is_intra_state=is_intra_state,
        requires_doctor=requires_doctor,
    )


def resolve_customer(
    db: Session, *, store_id: UUID, payload, actor_id: UUID
) -> Customer | None:
    """Find or quick-create the customer. A bare mobile is enough.

    Returns None for a true walk-in; the mobile is still snapshotted onto the
    invoice by the caller, so the register can find it either way.
    """
    if payload is None:
        return None

    customer_id = getattr(payload, "customer_id", None)
    if customer_id:
        customer = db.get(Customer, customer_id)
        if customer is None or customer.store_id != store_id:
            raise NotFoundError("Customer not found.", field="customer_id")
        return customer

    mobile = (getattr(payload, "mobile", None) or "").strip()
    if not mobile:
        return None

    customer = (
        db.query(Customer)
        .filter(Customer.store_id == store_id, Customer.mobile == mobile)
        .one_or_none()
    )
    name = (getattr(payload, "name", None) or "").strip() or None
    if customer is None:
        customer = Customer(
            store_id=store_id,
            mobile=mobile,
            name=name,
            gstin=(getattr(payload, "gstin", None) or None),
            state_code=(getattr(payload, "state_code", None) or None),
            created_by_id=actor_id,
            updated_by_id=actor_id,
        )
        db.add(customer)
        db.flush()
    elif name and not customer.name:
        # The counter learns the name on a later visit; fill the gap without
        # overwriting anything already recorded.
        customer.name = name
    return customer


def resolve_customer_readonly(db: Session, *, store_id: UUID, payload) -> Customer | None:
    """Look a customer up without creating one — quoting must not write.

    A mobile the shop has never seen simply quotes as a walk-in; the
    customer row is created when the sale is confirmed, not while the
    operator is still typing.
    """
    if payload is None:
        return None
    if getattr(payload, "customer_id", None):
        customer = db.get(Customer, payload.customer_id)
        if customer is None or customer.store_id != store_id:
            raise NotFoundError("Customer not found.", field="customer_id")
        return customer
    mobile = (getattr(payload, "mobile", None) or "").strip()
    if not mobile:
        return None
    return (
        db.query(Customer)
        .filter(Customer.store_id == store_id, Customer.mobile == mobile)
        .one_or_none()
    )


def find_by_idempotency_key(db: Session, store_id: UUID, key: str | None) -> Sale | None:
    """A retried submit must return the first invoice, not bill twice."""
    if not key:
        return None
    return (
        db.query(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .filter(Sale.store_id == store_id, Sale.idempotency_key == key)
        .one_or_none()
    )


def confirm_sale(
    db: Session,
    *,
    store_id: UUID,
    payload,
    settings: StoreSettings,
    user: User,
) -> Sale:
    """Issue an invoice: allocate, price, decrement stock, take payment."""
    customer = resolve_customer(
        db, store_id=store_id, payload=payload.customer, actor_id=user.id
    )

    doctor = None
    if payload.doctor_id:
        doctor = db.get(Doctor, payload.doctor_id)
        if doctor is None or doctor.store_id != store_id:
            raise NotFoundError("Doctor not found.", field="doctor_id")

    quote = build_quote(
        db,
        store_id=store_id,
        items=payload.items,
        settings=settings,
        customer=customer,
        doctor_id=payload.doctor_id,
        is_admin=user.is_admin,
        discount_override_reason=payload.discount_override_reason,
    )

    _check_payments(payload.payments, quote.totals.net_amount)

    now = datetime.now(timezone.utc)
    sale = Sale(
        store_id=store_id,
        invoice_no=sequences.next_document_number(db, store_id, DocumentType.SALE),
        # The instant is UTC; the *date on the bill* is the shop's own, and
        # is derived from that same instant so the two can never disagree —
        # a sale at 01:00 IST belongs to the day that has just begun, not to
        # the one still running in UTC.
        invoice_date=to_business_date(now),
        invoice_datetime=now,
        customer_id=customer.id if customer else None,
        customer_mobile_snapshot=(
            customer.mobile if customer else _walk_in_mobile(payload.customer)
        ),
        customer_name_snapshot=(
            customer.name if customer else _walk_in_name(payload.customer)
        ),
        customer_gstin_snapshot=customer.gstin if customer else None,
        doctor_id=doctor.id if doctor else None,
        doctor_name_snapshot=doctor.name if doctor else None,
        prescription_ref=payload.prescription_ref,
        sales_user_id=user.id,
        sales_user_name=user.full_name,
        status=SaleStatus.COMPLETED.value,
        is_intra_state=quote.is_intra_state,
        idempotency_key=payload.idempotency_key,
        discount_override_reason=payload.discount_override_reason,
        notes=payload.notes,
        gross_amount=quote.totals.gross_amount,
        discount_amount=quote.totals.discount_amount,
        taxable_amount=quote.totals.taxable_amount,
        cgst_amount=quote.totals.cgst_amount,
        sgst_amount=quote.totals.sgst_amount,
        igst_amount=quote.totals.igst_amount,
        round_off=quote.totals.round_off,
        net_amount=quote.totals.net_amount,
        paid_amount=quote.totals.net_amount,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(sale)
    db.flush()

    for line in quote.lines:
        medicine, batch, price = line.medicine, line.batch, line.price
        item = SaleItem(
            sale_id=sale.id,
            line_no=line.line_no,
            medicine_id=medicine.id,
            batch_id=batch.id,
            # Snapshots: the invoice must survive any later edit to the
            # catalogue or the batch.
            medicine_name_snapshot=medicine.brand_name,
            pack_label_snapshot=medicine.pack_label,
            hsn_code_snapshot=medicine.hsn_code,
            item_type_snapshot=medicine.item_type,
            batch_no_snapshot=batch.batch_no,
            expiry_date_snapshot=batch.expiry_date,
            units_per_pack_snapshot=batch.units_per_pack,
            qty_units=price.qty_units,
            unit_mrp=price.unit_mrp,
            mrp_per_pack=batch.mrp_per_pack,
            gross_amount=price.gross_amount,
            discount_pct=price.discount_pct,
            discount_amount=price.discount_amount,
            net_amount=price.net_amount,
            gst_rate=price.gst_rate,
            taxable_value=price.taxable_value,
            cgst_amount=price.cgst_amount,
            sgst_amount=price.sgst_amount,
            igst_amount=price.igst_amount,
            unit_cost_snapshot=batch.unit_cost,
        )
        db.add(item)
        db.flush()

        inventory.post_movement(
            db,
            batch=batch,
            movement_type=MovementType.SALE,
            qty_units_delta=-price.qty_units,
            actor_id=user.id,
            reference_type=ReferenceType.SALE_ITEM,
            reference_id=item.id,
            reason=f"Invoice {sale.invoice_no}",
        )

    for entry in payload.payments:
        payment = Payment(
            sale_id=sale.id,
            mode=str(entry.mode),
            amount=quantize_money(entry.amount),
            reference_no=entry.reference_no,
            credit_note_id=entry.credit_note_id,
            received_at=now,
            created_by_id=user.id,
            updated_by_id=user.id,
        )
        db.add(payment)
        db.flush()

        if entry.mode == PaymentMode.CREDIT_NOTE:
            # Inside the sale transaction and under a row lock, so the same
            # note cannot be spent twice by two tills at once. A failure
            # here rolls the whole invoice back.
            credit_notes.redeem(
                db,
                note_id=entry.credit_note_id,
                store_id=store_id,
                customer=customer,
                amount=entry.amount,
                sale_id=sale.id,
                payment_id=payment.id,
                actor_id=user.id,
            )

    db.flush()
    return sale


def _check_payments(payments, net_amount: Decimal) -> None:
    """Split payments must add up to the payable, to the paisa."""
    if not payments:
        raise SaleError("Record how the invoice was paid.", field="payments")

    for entry in payments:
        if entry.mode == PaymentMode.CREDIT_NOTE and not entry.credit_note_id:
            raise SaleError(
                "Say which credit note is being redeemed.",
                code="CREDIT_NOTE_REQUIRED",
                field="payments",
            )
        if entry.amount <= ZERO:
            raise SaleError("A payment must be more than zero.", field="payments")

    paid = quantize_money(sum((Decimal(p.amount) for p in payments), ZERO))
    if paid != net_amount:
        raise SaleError(
            f"Payments total {paid} but the invoice is {net_amount}.",
            code="PAYMENT_MISMATCH",
            field="payments",
        )


def _walk_in_mobile(payload) -> str | None:
    mobile = (getattr(payload, "mobile", None) or "").strip() if payload else ""
    return mobile or None


def _walk_in_name(payload) -> str | None:
    name = (getattr(payload, "name", None) or "").strip() if payload else ""
    return name or None


def _add_days(value, days: int):
    from datetime import timedelta

    return value + timedelta(days=days)
