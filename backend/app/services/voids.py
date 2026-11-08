"""Cancelling a whole invoice, under approval.

A sale is a financial document, so it is never deleted. Voiding is a
two-step act: whoever billed it *requests* the void with a reason, and an
admin approves. That separation is the control — the person who took the
money cannot also make the record of it disappear.

Approval puts every unit back in the batch it came from, through fresh
ledger rows rather than by rewriting the old ones. The invoice number stays
on the sale and is never reissued, so a gap-free sequence still tells the
truth about how many invoices were raised.
"""

from uuid import UUID

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ConflictError
from app.db.types import quantize_qty
from app.models.enums import ApprovalStatus, MovementType, ReferenceType, SaleStatus
from app.models.inventory import Batch
from app.models.returns import SaleVoidRequest
from app.models.sale import Sale
from app.models.user import User
from app.services import inventory

ZERO_QTY = Decimal("0.000")

# A sale in any of these states has already been settled one way or the
# other and cannot be voided.
CLOSED_STATUSES = (SaleStatus.VOIDED,)


class VoidError(AppError):
    code = "VOID_INVALID"


def request_void(
    db: Session, *, sale: Sale, reason: str, user: User
) -> SaleVoidRequest:
    """Ask for an invoice to be cancelled."""
    _assert_voidable(db, sale)

    open_request = _open_request(db, sale.id)
    if open_request is not None:
        raise ConflictError(
            f"A void request for invoice {sale.invoice_no} is already waiting for "
            f"approval.",
            code="VOID_ALREADY_REQUESTED",
        )

    now = datetime.now(timezone.utc)
    request = SaleVoidRequest(
        store_id=sale.store_id,
        sale_id=sale.id,
        requested_by_id=user.id,
        requested_by_name=user.full_name,
        request_reason=reason,
        requested_at=now,
        status=ApprovalStatus.PENDING.value,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(request)

    sale.status = SaleStatus.VOID_REQUESTED.value
    sale.updated_by_id = user.id
    db.flush()
    return request


def approve_void(
    db: Session, *, request: SaleVoidRequest, note: str | None, admin: User
) -> Sale:
    """Approve: return every unit to its batch and mark the sale void."""
    _assert_pending(request)

    sale = db.get(Sale, request.sale_id)
    if sale is None:
        raise VoidError("The sale behind this request no longer exists.", status_code=404)
    _assert_voidable(db, sale, allow_requested=True)

    for item in sale.items:
        batch = db.get(Batch, item.batch_id)
        if batch is None:
            raise VoidError(
                f"The batch behind {item.medicine_name_snapshot} is missing, so the "
                f"stock cannot be put back.",
                code="BATCH_MISSING",
            )
        inventory.post_movement(
            db,
            batch=batch,
            movement_type=MovementType.VOID_REVERSAL,
            qty_units_delta=item.qty_units,
            actor_id=admin.id,
            reference_type=ReferenceType.SALE_ITEM,
            reference_id=item.id,
            reason=f"Void of invoice {sale.invoice_no}",
        )
        # post_movement grows qty_received on the way in; a reversal is not
        # a receipt, so undo that part of it.
        batch.qty_received_units = quantize_qty(batch.qty_received_units - item.qty_units)

    now = datetime.now(timezone.utc)
    request.status = ApprovalStatus.APPROVED.value
    request.reviewed_by_id = admin.id
    request.reviewed_by_name = admin.full_name
    request.review_note = note
    request.reviewed_at = now
    request.updated_by_id = admin.id

    # The number stays on the sale and is never reissued: the sequence has
    # to keep telling the truth about how many invoices were raised.
    sale.status = SaleStatus.VOIDED.value
    sale.updated_by_id = admin.id
    db.flush()
    return sale


def reject_void(
    db: Session, *, request: SaleVoidRequest, note: str, admin: User
) -> Sale:
    """Refuse the request; the sale goes back to being an ordinary invoice."""
    _assert_pending(request)

    sale = db.get(Sale, request.sale_id)
    now = datetime.now(timezone.utc)
    request.status = ApprovalStatus.REJECTED.value
    request.reviewed_by_id = admin.id
    request.reviewed_by_name = admin.full_name
    request.review_note = note
    request.reviewed_at = now
    request.updated_by_id = admin.id

    if sale is not None:
        sale.status = _status_after_rejection(sale)
        sale.updated_by_id = admin.id
    db.flush()
    return sale


def _status_after_rejection(sale: Sale) -> str:
    """Back to COMPLETED — or to whichever returned state it was in."""
    returned = sum((item.returned_qty_units for item in sale.items), ZERO_QTY)
    if returned <= ZERO_QTY:
        return SaleStatus.COMPLETED.value
    sold = sum((item.qty_units for item in sale.items), ZERO_QTY)
    return (
        SaleStatus.RETURNED.value
        if returned >= sold
        else SaleStatus.PARTIALLY_RETURNED.value
    )


def _open_request(db: Session, sale_id: UUID) -> SaleVoidRequest | None:
    return (
        db.query(SaleVoidRequest)
        .filter(
            SaleVoidRequest.sale_id == sale_id,
            SaleVoidRequest.status == ApprovalStatus.PENDING.value,
        )
        .one_or_none()
    )


def _assert_pending(request: SaleVoidRequest) -> None:
    if request.status != ApprovalStatus.PENDING.value:
        raise ConflictError(
            f"This request was already {request.status.lower()}.",
            code="VOID_ALREADY_REVIEWED",
        )


def _assert_voidable(db: Session, sale: Sale, allow_requested: bool = False) -> None:
    if sale.status == SaleStatus.VOIDED.value:
        raise ConflictError(
            f"Invoice {sale.invoice_no} is already void.", code="SALE_ALREADY_VOIDED"
        )
    if sale.status == SaleStatus.VOID_REQUESTED.value and not allow_requested:
        raise ConflictError(
            f"A void request for invoice {sale.invoice_no} is already waiting for approval.",
            code="VOID_ALREADY_REQUESTED",
        )

    # Reversing a sale whose goods have partly come back would double-count
    # the stock: the return already gave those units to the batch.
    returned = sum((item.returned_qty_units for item in sale.items), ZERO_QTY)
    if returned > ZERO_QTY:
        raise ConflictError(
            f"Invoice {sale.invoice_no} has goods returned against it. Reverse the "
            f"return first — voiding now would put the same stock back twice.",
            code="SALE_HAS_RETURNS",
        )
