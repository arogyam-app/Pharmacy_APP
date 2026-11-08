"""Choosing which batch a sale draws from.

Default is **FEFO** — first expiry, first out. Stock that expires soonest
leaves the shelf first, which is what keeps a pharmacy from writing off
stock it could have sold. The operator may pin a specific batch (a customer
wants the lot matching an earlier strip), but two rules hold either way:

* **expired stock can never be sold** — a hard block with no override;
* **quarantined stock is invisible** — a batch under `is_blocked` is out of
  circulation until someone clears it.

One cart line can outgrow one batch, so allocation returns a list of slices.
Each slice becomes its own `sale_item`: the batches carry different expiries
and sometimes different MRPs, and a return has to give each batch back
exactly the units it gave up.
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.db.types import quantize_qty
from app.models.inventory import Batch
from app.utils.clock import business_date

ZERO = Decimal("0.000")


class AllocationError(AppError):
    code = "INSUFFICIENT_STOCK"


class Slice:
    """`qty_units` taken from one batch."""

    __slots__ = ("batch", "qty_units")

    def __init__(self, batch: Batch, qty_units: Decimal) -> None:
        self.batch = batch
        self.qty_units = qty_units


def sellable_today(batch: Batch, today: date) -> bool:
    return (
        not batch.is_blocked
        and batch.expiry_date >= today
        and batch.qty_available_units > ZERO
    )


def fefo_batches(db: Session, store_id: UUID, medicine_id: UUID) -> list[Batch]:
    """Sellable batches, soonest expiry first, locked for update.

    The row lock matters: two counters selling the last strip at the same
    moment must queue, or both would read the same availability and the
    ledger would go negative. `with_for_update()` is a no-op on SQLite and
    real on PostgreSQL (CLAUDE.md rule 7).
    """
    today = business_date()
    rows = (
        db.query(Batch)
        .filter(
            Batch.store_id == store_id,
            Batch.medicine_id == medicine_id,
            Batch.is_blocked.is_(False),
            Batch.expiry_date >= today,
        )
        .order_by(Batch.expiry_date, Batch.id)
        .with_for_update()
        .all()
    )
    return [b for b in rows if b.qty_available_units > ZERO]


def allocate(
    db: Session,
    *,
    store_id: UUID,
    medicine_id: UUID,
    qty_units: Decimal,
    medicine_name: str,
    batch_id: UUID | None = None,
    field: str = "qty_units",
) -> list[Slice]:
    """Decide which batches supply `qty_units`, or explain why they cannot."""
    wanted = quantize_qty(qty_units)
    if wanted <= ZERO:
        raise AllocationError(
            f"{medicine_name}: quantity must be more than zero.",
            code="INVALID_QUANTITY",
            field=field,
        )

    today = business_date()

    if batch_id is not None:
        batch = db.get(Batch, batch_id)
        if batch is None or batch.store_id != store_id or batch.medicine_id != medicine_id:
            raise AllocationError(
                f"{medicine_name}: that batch does not belong to this medicine.",
                code="NOT_FOUND",
                status_code=404,
                field="batch_id",
            )
        # Expiry and quarantine are checked before availability: "it expired"
        # is the useful message, not "not enough stock".
        if batch.expiry_date < today:
            raise AllocationError(
                f"{medicine_name} batch {batch.batch_no} expired on {batch.expiry_date} "
                f"and cannot be sold.",
                code="BATCH_EXPIRED",
                field="batch_id",
            )
        if batch.is_blocked:
            raise AllocationError(
                f"{medicine_name} batch {batch.batch_no} is blocked"
                + (f" ({batch.block_reason})." if batch.block_reason else "."),
                code="BATCH_BLOCKED",
                field="batch_id",
            )
        if batch.qty_available_units < wanted:
            raise AllocationError(
                f"{medicine_name} batch {batch.batch_no} has only "
                f"{_plain(batch.qty_available_units)} unit(s) left.",
                field=field,
            )
        return [Slice(batch, wanted)]

    batches = fefo_batches(db, store_id, medicine_id)
    available = quantize_qty(sum((b.qty_available_units for b in batches), ZERO))
    if available < wanted:
        raise AllocationError(
            f"{medicine_name}: only {_plain(available)} sellable unit(s) in stock, "
            f"{_plain(wanted)} requested."
            + _expired_hint(db, store_id, medicine_id, today),
            field=field,
        )

    slices: list[Slice] = []
    remaining = wanted
    for batch in batches:
        if remaining <= ZERO:
            break
        take = min(batch.qty_available_units, remaining)
        slices.append(Slice(batch, quantize_qty(take)))
        remaining = quantize_qty(remaining - take)
    return slices


def _expired_hint(db: Session, store_id: UUID, medicine_id: UUID, today: date) -> str:
    """Explain stock that exists but cannot be sold, rather than let the
    operator stare at a shelf that plainly has boxes on it."""
    held = (
        db.query(Batch)
        .filter(
            Batch.store_id == store_id,
            Batch.medicine_id == medicine_id,
            Batch.qty_available_units > ZERO,
        )
        .all()
    )
    expired = sum((b.qty_available_units for b in held if b.expiry_date < today), ZERO)
    blocked = sum((b.qty_available_units for b in held if b.is_blocked), ZERO)

    parts = []
    if expired > ZERO:
        parts.append(f"{_plain(expired)} expired")
    if blocked > ZERO:
        parts.append(f"{_plain(blocked)} blocked")
    return f" ({' and '.join(parts)} unit(s) are held but not sellable.)" if parts else ""


def _plain(value: Decimal) -> str:
    """Drop trailing zeros: 12.000 -> 12, 0.500 -> 0.5."""
    trimmed = Decimal(value).normalize()
    return f"{trimmed:f}"
