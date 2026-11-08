"""Sending goods back to a distributor, and spending the credit that creates.

Three things happen when stock goes back, and they must happen together or
not at all:

1. The units leave the batch, through the ledger like every other movement.
2. The input tax claimed when those units arrived is reversed, at the rate and
   the cost they arrived at — not at today's cost and not at MRP.
3. A credit appears against that distributor, to be set against a later
   invoice from them.

Step 3 is where the trade and the tax law disagree about what a document is.
Commercially the distributor "adjusts it in the next bill"; for GST the return
and the next purchase are two documents, each with its own tax. So the credit
is held here, on the debit note, and applied to an invoice as a *payment*
— the invoice's taxable value and input tax credit are untouched by it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.db.types import ZERO_MONEY as ZERO
from app.db.types import quantize_money
from app.models.enums import (
    DocumentType,
    MovementType,
    PurchaseReturnStatus,
    ReferenceType,
    SupplierCreditStatus,
)
from app.models.inventory import Batch
from app.models.party import Supplier
from app.models.purchase import PurchaseInvoice, PurchaseItem
from app.models.purchase_return import (
    PurchaseReturn,
    PurchaseReturnItem,
    SupplierCreditUsage,
)
from app.models.store import Store, StoreSettings
from app.models.user import User
from app.services import inventory as inventory_service
from app.services import sequences
from app.services.purchases import compute_line_tax, resolve_intra_state
from app.utils.clock import business_date


class PurchaseReturnError(AppError):
    code = "PURCHASE_RETURN_INVALID"


def _supplier(db: Session, store_id: UUID, supplier_id: UUID) -> Supplier:
    supplier = (
        db.query(Supplier)
        .filter(Supplier.id == supplier_id, Supplier.store_id == store_id)
        .one_or_none()
    )
    if supplier is None:
        raise NotFoundError("Supplier not found.", field="supplier_id")
    return supplier


def _batch_for_return(db: Session, store_id: UUID, batch_id: UUID) -> Batch:
    """The batch, locked, so two returns cannot send the same units back."""
    batch = (
        db.query(Batch)
        .filter(Batch.id == batch_id, Batch.store_id == store_id)
        .with_for_update()
        .one_or_none()
    )
    if batch is None:
        raise NotFoundError("Batch not found.", field="batch_id")
    return batch


def create_return(
    db: Session,
    *,
    store_id: UUID,
    payload,
    settings: StoreSettings,
    user: User,
) -> PurchaseReturn:
    """Raise a debit note for one or more batches going back to a supplier.

    Expired stock is allowed: a take-back of short-dated goods is one of the
    commonest reasons to raise one of these, and refusing it would push the
    operator into writing the stock off instead — which loses the credit.
    """
    supplier = _supplier(db, store_id, payload.supplier_id)
    store = db.get(Store, store_id)
    is_intra_state = resolve_intra_state(store, supplier, None)

    invoice = None
    if payload.purchase_invoice_id is not None:
        invoice = (
            db.query(PurchaseInvoice)
            .filter(
                PurchaseInvoice.id == payload.purchase_invoice_id,
                PurchaseInvoice.store_id == store_id,
            )
            .one_or_none()
        )
        if invoice is None:
            raise NotFoundError("Purchase invoice not found.", field="purchase_invoice_id")
        if invoice.supplier_id != supplier.id:
            raise PurchaseReturnError(
                "That invoice belongs to a different supplier.",
                field="purchase_invoice_id",
            )

    document = PurchaseReturn(
        store_id=store_id,
        debit_note_no=sequences.next_document_number(
            db, store_id, DocumentType.PURCHASE_RETURN
        ),
        supplier_id=supplier.id,
        purchase_invoice_id=invoice.id if invoice else None,
        return_date=business_date(),
        reason=payload.reason.strip(),
        supplier_credit_note_ref=(payload.supplier_credit_note_ref or "").strip() or None,
        is_intra_state=is_intra_state,
        notes=(payload.notes or "").strip() or None,
        status=PurchaseReturnStatus.COMPLETED.value,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(document)
    db.flush()

    totals = {"taxable": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO, "total": ZERO}

    for line_no, line in enumerate(payload.items, start=1):
        batch = _batch_for_return(db, store_id, line.batch_id)

        if line.qty_units <= 0:
            raise PurchaseReturnError(
                "A return quantity must be more than zero.", field="qty_units"
            )
        if line.qty_units > batch.qty_available_units:
            raise ConflictError(
                f"Batch {batch.batch_no} holds {batch.qty_available_units} unit(s); "
                f"cannot return {line.qty_units}.",
                code="INSUFFICIENT_STOCK",
                field="qty_units",
            )

        # Goods go back at what they came in at. `unit_cost` is GST-exclusive,
        # which is what the input tax was computed on, so reversing at the
        # same figure reverses exactly what was claimed.
        cost = line.cost_per_unit if line.cost_per_unit is not None else batch.unit_cost
        if cost is None:
            raise PurchaseReturnError(
                f"Batch {batch.batch_no} has no cost recorded, so there is nothing to "
                f"credit. Enter the cost on the line.",
                field="cost_per_unit",
            )

        # The same helper the purchase used, so the return rounds identically
        # and the reversal cannot drift a paisa from the claim. It is written
        # in packs, but the arithmetic is quantity × price either way — here
        # the quantity is units and the price is per unit.
        tax = compute_line_tax(
            qty_packs=line.qty_units,
            cost_per_pack=Decimal(cost),
            discount_pct=ZERO,
            gst_rate=batch.gst_rate,
            is_intra_state=is_intra_state,
        )

        medicine = batch.medicine
        item = PurchaseReturnItem(
            purchase_return_id=document.id,
            line_no=line_no,
            medicine_id=batch.medicine_id,
            batch_id=batch.id,
            purchase_item_id=_source_line(db, batch, invoice),
            medicine_name_snapshot=medicine.brand_name,
            pack_label_snapshot=medicine.pack_label,
            hsn_code_snapshot=medicine.hsn_code,
            batch_no_snapshot=batch.batch_no,
            expiry_date_snapshot=batch.expiry_date,
            qty_units=line.qty_units,
            cost_per_unit=Decimal(cost),
            gst_rate=batch.gst_rate,
            taxable_value=tax.taxable_value,
            cgst_amount=tax.cgst,
            sgst_amount=tax.sgst,
            igst_amount=tax.igst,
            line_total=tax.line_total,
        )
        db.add(item)
        db.flush()

        inventory_service.post_movement(
            db,
            batch=batch,
            movement_type=MovementType.PURCHASE_RETURN,
            qty_units_delta=-line.qty_units,
            actor_id=user.id,
            reference_type=ReferenceType.PURCHASE_RETURN_ITEM,
            reference_id=item.id,
            reason=f"Returned to {supplier.name} on debit note {document.debit_note_no}",
        )

        totals["taxable"] += tax.taxable_value
        totals["cgst"] += tax.cgst
        totals["sgst"] += tax.sgst
        totals["igst"] += tax.igst
        totals["total"] += tax.line_total

    if not document.items and not totals["total"]:
        raise PurchaseReturnError("A return needs at least one line.", field="items")

    document.taxable_amount = quantize_money(totals["taxable"])
    document.cgst_amount = quantize_money(totals["cgst"])
    document.sgst_amount = quantize_money(totals["sgst"])
    document.igst_amount = quantize_money(totals["igst"])
    document.total_amount = quantize_money(totals["total"])

    # The whole value is available from the moment the goods leave.
    document.credit_used = ZERO
    document.credit_balance = document.total_amount
    document.credit_status = SupplierCreditStatus.OPEN.value

    db.flush()
    return document


def _source_line(db: Session, batch: Batch, invoice: PurchaseInvoice | None) -> UUID | None:
    """The purchase line these units arrived on, when it can be identified."""
    if invoice is None:
        return None
    row = (
        db.query(PurchaseItem)
        .filter(
            PurchaseItem.purchase_invoice_id == invoice.id,
            PurchaseItem.batch_id == batch.id,
        )
        .first()
    )
    return row.id if row else None


# --- spending the credit --------------------------------------------------


def available_credit(db: Session, store_id: UUID, supplier_id: UUID) -> list[PurchaseReturn]:
    """Debit notes with something still on them, oldest first.

    Oldest first because that is how a distributor's ledger settles: the
    earliest credit is the one both sides have been carrying longest.
    """
    return (
        db.query(PurchaseReturn)
        .filter(
            PurchaseReturn.store_id == store_id,
            PurchaseReturn.supplier_id == supplier_id,
            PurchaseReturn.status == PurchaseReturnStatus.COMPLETED.value,
            PurchaseReturn.credit_status.in_(
                [SupplierCreditStatus.OPEN.value, SupplierCreditStatus.PARTIALLY_USED.value]
            ),
        )
        .order_by(PurchaseReturn.return_date, PurchaseReturn.debit_note_no)
        .all()
    )


def apply_credit(
    db: Session,
    *,
    store_id: UUID,
    purchase_return_id: UUID,
    invoice: PurchaseInvoice,
    amount: Decimal,
    user: User,
) -> SupplierCreditUsage:
    """Set part or all of a debit note against a purchase invoice.

    The row is locked before its balance is read, so two invoices settled at
    the same moment cannot both spend the same credit — the same reasoning as
    `credit_notes.spend` on the customer side.

    This does not touch the invoice's taxable value or its input tax. A credit
    is a settlement of what is owed, not a discount on what was bought; the
    invoice still claims the tax the distributor charged on it.
    """
    document = (
        db.query(PurchaseReturn)
        .filter(PurchaseReturn.id == purchase_return_id, PurchaseReturn.store_id == store_id)
        .with_for_update()
        .one_or_none()
    )
    if document is None:
        raise NotFoundError("Debit note not found.", field="purchase_return_id")

    if document.status != PurchaseReturnStatus.COMPLETED.value:
        raise ConflictError(
            f"Debit note {document.debit_note_no} was cancelled and carries no credit.",
            code="CREDIT_NOT_AVAILABLE",
        )
    if document.supplier_id != invoice.supplier_id:
        raise PurchaseReturnError(
            f"Debit note {document.debit_note_no} is against a different supplier.",
            field="purchase_return_id",
        )

    amount = quantize_money(Decimal(amount))
    if amount <= ZERO:
        raise PurchaseReturnError("Amount must be more than zero.", field="amount")
    if amount > document.credit_balance:
        raise ConflictError(
            f"Debit note {document.debit_note_no} has only "
            f"{document.credit_balance} left, not {amount}.",
            code="CREDIT_EXCEEDED",
            field="amount",
        )

    usage = SupplierCreditUsage(
        purchase_return_id=document.id,
        purchase_invoice_id=invoice.id,
        amount_used=amount,
        used_at=datetime.now(timezone.utc),
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(usage)

    document.credit_used = quantize_money(document.credit_used + amount)
    document.credit_balance = quantize_money(document.total_amount - document.credit_used)
    document.credit_status = (
        SupplierCreditStatus.EXHAUSTED.value
        if document.credit_balance <= ZERO
        else SupplierCreditStatus.PARTIALLY_USED.value
    )
    db.flush()
    return usage


def credit_applied_to(db: Session, invoice_id: UUID) -> Decimal:
    """How much supplier credit has been set against one invoice."""
    rows = (
        db.query(SupplierCreditUsage.amount_used)
        .filter(SupplierCreditUsage.purchase_invoice_id == invoice_id)
        .all()
    )
    return quantize_money(sum((r[0] for r in rows), ZERO))


# --- undoing one ----------------------------------------------------------


def cancel_return(
    db: Session, *, store_id: UUID, purchase_return_id: UUID, reason: str, user: User
) -> PurchaseReturn:
    """Put the goods back and withdraw the credit.

    Refused once any of the credit has been spent: that money is already
    reflected on a purchase invoice, and unwinding it here would leave the
    invoice claiming a settlement that no longer exists. Raise a fresh
    purchase instead — the same rule the sales side applies to a credit note
    that has been used (CLAUDE.md rule 5).
    """
    document = (
        db.query(PurchaseReturn)
        .filter(PurchaseReturn.id == purchase_return_id, PurchaseReturn.store_id == store_id)
        .with_for_update()
        .one_or_none()
    )
    if document is None:
        raise NotFoundError("Debit note not found.")

    if document.status == PurchaseReturnStatus.CANCELLED.value:
        raise ConflictError(
            f"Debit note {document.debit_note_no} is already cancelled.",
            code="ALREADY_CANCELLED",
        )
    if document.credit_used > ZERO:
        raise ConflictError(
            f"{document.credit_used} of debit note {document.debit_note_no} has already been "
            f"set against a purchase invoice. It cannot be cancelled.",
            code="CREDIT_ALREADY_USED",
        )

    for item in document.items:
        batch = _batch_for_return(db, store_id, item.batch_id)
        inventory_service.post_movement(
            db,
            batch=batch,
            movement_type=MovementType.PURCHASE_RETURN,
            qty_units_delta=item.qty_units,          # back in
            actor_id=user.id,
            reference_type=ReferenceType.PURCHASE_RETURN_ITEM,
            reference_id=item.id,
            reason=f"Debit note {document.debit_note_no} cancelled: {reason}",
        )

    document.status = PurchaseReturnStatus.CANCELLED.value
    document.credit_status = SupplierCreditStatus.CANCELLED.value
    document.credit_balance = ZERO
    document.cancelled_at = datetime.now(timezone.utc)
    document.cancelled_by_id = user.id
    document.cancel_reason = reason
    document.updated_by_id = user.id
    db.flush()
    return document
