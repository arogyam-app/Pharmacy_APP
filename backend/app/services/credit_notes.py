"""Credit notes — money owed back to a customer, spendable on a later sale.

A note belongs to a customer, which is why a walk-in return cannot issue
one: with no mobile number there is no way to know who is entitled to spend
it. The counter takes a number first, or refunds cash.

`balance_amount` is a cached figure kept in step with `credit_note_usages`,
the same arrangement as batches and the stock ledger: the usages are the
truth and a test asserts the two agree. Redemption happens inside the sale
transaction under a row lock, so the same note cannot be spent twice by two
tills at once.
"""

from uuid import UUID

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.db.types import quantize_money
from app.models.enums import CreditNoteStatus, DocumentType
from app.models.party import Customer
from app.models.returns import CreditNote, CreditNoteUsage
from app.models.store import StoreSettings
from app.services import sequences
from app.utils.clock import business_date

ZERO = Decimal("0.00")


class CreditNoteError(AppError):
    code = "CREDIT_NOTE_INVALID"


def issue(
    db: Session,
    *,
    store_id: UUID,
    customer: Customer,
    amount: Decimal,
    settings: StoreSettings,
    sale_return_id: UUID | None = None,
    actor_id: UUID | None = None,
    notes: str | None = None,
) -> CreditNote:
    """Raise a note for `amount` against a customer."""
    value = quantize_money(amount)
    if value <= ZERO:
        raise CreditNoteError("A credit note must be for more than zero.", field="amount")

    today = business_date()
    note = CreditNote(
        store_id=store_id,
        credit_note_no=sequences.next_document_number(db, store_id, DocumentType.CREDIT_NOTE),
        customer_id=customer.id,
        sale_return_id=sale_return_id,
        issue_date=today,
        amount_issued=value,
        amount_used=ZERO,
        balance_amount=value,
        expires_on=today + timedelta(days=settings.credit_note_validity_days),
        status=CreditNoteStatus.OPEN.value,
        notes=notes,
        created_by_id=actor_id,
        updated_by_id=actor_id,
    )
    db.add(note)
    db.flush()

    _refresh_customer_balance(db, customer)
    return note


def open_notes(db: Session, *, store_id: UUID, customer_id: UUID) -> list[CreditNote]:
    """Notes this customer can still spend, oldest first so the ones nearest
    expiry are used up before they lapse."""
    today = business_date()
    rows = (
        db.query(CreditNote)
        .filter(
            CreditNote.store_id == store_id,
            CreditNote.customer_id == customer_id,
            CreditNote.status.in_(
                [CreditNoteStatus.OPEN.value, CreditNoteStatus.PARTIALLY_USED.value]
            ),
        )
        .order_by(CreditNote.expires_on, CreditNote.id)
        .all()
    )
    return [n for n in rows if n.balance_amount > ZERO and n.expires_on >= today]


def redeem(
    db: Session,
    *,
    note_id: UUID,
    store_id: UUID,
    customer: Customer | None,
    amount: Decimal,
    sale_id: UUID,
    payment_id: UUID | None,
    actor_id: UUID | None = None,
) -> CreditNoteUsage:
    """Spend `amount` from a note. Locks the row, so it cannot go twice."""
    note = (
        db.query(CreditNote)
        .filter(CreditNote.id == note_id, CreditNote.store_id == store_id)
        .with_for_update()
        .one_or_none()
    )
    if note is None:
        raise NotFoundError("Credit note not found.", field="credit_note_id")

    if customer is None or note.customer_id != customer.id:
        raise CreditNoteError(
            f"Credit note {note.credit_note_no} belongs to a different customer.",
            code="CREDIT_NOTE_WRONG_CUSTOMER",
            field="credit_note_id",
        )

    today = business_date()
    if note.status == CreditNoteStatus.CANCELLED.value:
        raise ConflictError(
            f"Credit note {note.credit_note_no} was cancelled.",
            code="CREDIT_NOTE_CANCELLED",
        )
    if note.expires_on < today:
        # Mark it, so the register stops offering an expired note.
        note.status = CreditNoteStatus.EXPIRED.value
        raise ConflictError(
            f"Credit note {note.credit_note_no} expired on {note.expires_on}.",
            code="CREDIT_NOTE_EXPIRED",
        )

    wanted = quantize_money(amount)
    if wanted <= ZERO:
        raise CreditNoteError("A redemption must be for more than zero.", field="amount")
    if wanted > note.balance_amount:
        raise ConflictError(
            f"Credit note {note.credit_note_no} has {note.balance_amount} left, "
            f"but {wanted} was applied.",
            code="CREDIT_NOTE_INSUFFICIENT_BALANCE",
            field="payments",
        )

    usage = CreditNoteUsage(
        credit_note_id=note.id,
        sale_id=sale_id,
        payment_id=payment_id,
        amount_used=wanted,
        used_at=datetime.now(timezone.utc),
        created_by_id=actor_id,
        updated_by_id=actor_id,
    )
    db.add(usage)

    note.amount_used = quantize_money(note.amount_used + wanted)
    note.balance_amount = quantize_money(note.amount_issued - note.amount_used)
    note.status = (
        CreditNoteStatus.FULLY_USED.value
        if note.balance_amount <= ZERO
        else CreditNoteStatus.PARTIALLY_USED.value
    )
    note.updated_by_id = actor_id
    db.flush()

    if customer is not None:
        _refresh_customer_balance(db, customer)
    return usage


def cancel(
    db: Session, *, note: CreditNote, reason: str, actor_id: UUID | None = None
) -> CreditNote:
    """Withdraw a note that has not been spent."""
    if note.status == CreditNoteStatus.CANCELLED.value:
        raise ConflictError(
            f"Credit note {note.credit_note_no} is already cancelled.",
            code="CREDIT_NOTE_CANCELLED",
        )
    if note.amount_used > ZERO:
        raise ConflictError(
            f"Credit note {note.credit_note_no} has {note.amount_used} already spent "
            f"against an invoice and cannot be cancelled.",
            code="CREDIT_NOTE_IN_USE",
        )

    note.status = CreditNoteStatus.CANCELLED.value
    note.balance_amount = ZERO
    note.cancelled_reason = reason
    note.updated_by_id = actor_id
    db.flush()

    customer = db.get(Customer, note.customer_id)
    if customer is not None:
        _refresh_customer_balance(db, customer)
    return note


def expire_lapsed(db: Session, *, store_id: UUID) -> int:
    """Mark notes whose date has passed. Cheap enough to run on read."""
    today = business_date()
    lapsed = (
        db.query(CreditNote)
        .filter(
            CreditNote.store_id == store_id,
            CreditNote.expires_on < today,
            CreditNote.status.in_(
                [CreditNoteStatus.OPEN.value, CreditNoteStatus.PARTIALLY_USED.value]
            ),
        )
        .all()
    )
    for note in lapsed:
        note.status = CreditNoteStatus.EXPIRED.value
    return len(lapsed)


def _refresh_customer_balance(db: Session, customer: Customer) -> None:
    """Keep the cached figure on the customer in step with their notes."""
    today = business_date()
    total = sum(
        (
            n.balance_amount
            for n in db.query(CreditNote).filter(
                CreditNote.customer_id == customer.id,
                CreditNote.status.in_(
                    [CreditNoteStatus.OPEN.value, CreditNoteStatus.PARTIALLY_USED.value]
                ),
                CreditNote.expires_on >= today,
            )
        ),
        ZERO,
    )
    customer.credit_balance = quantize_money(total)
