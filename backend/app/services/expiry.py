"""The expiry register: stock that went out of date on the shelf.

Expired stock is already unsellable — the POS blocks it outright — so this
register is not about preventing a sale. It is about closing the loss:
either write it off, which zeroes the stock through the ledger and books
the cost, or send it back to the distributor against a debit note.

Scanning is idempotent. One entry per batch, refreshed if the quantity has
moved, so running it twice a day costs nothing.
"""

from uuid import UUID

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.exceptions import AppError, ConflictError
from app.db.types import quantize_money
from app.models.catalog import Medicine
from app.models.enums import (
    ExpirySource,
    ExpiryStatus,
    MovementType,
    ReferenceType,
)
from app.models.inventory import Batch
from app.models.party import Supplier
from app.models.stockwatch import ExpiryRegisterEntry
from app.models.user import User
from app.services import alerts, inventory
from app.utils.clock import business_date

ZERO = Decimal("0.000")

# Once acted on, an entry is finished with.
CLOSED_STATUSES = (
    ExpiryStatus.WRITTEN_OFF.value,
    ExpiryStatus.RETURNED_TO_SUPPLIER.value,
    ExpiryStatus.CREDIT_RECEIVED.value,
)


class ExpiryError(AppError):
    code = "EXPIRY_INVALID"


def scan(db: Session, *, store_id: UUID, actor_id: UUID | None = None) -> dict:
    """Pull every expired batch that still holds stock into the register."""
    batches = alerts.expired_batches(db, store_id)
    existing = {
        row.batch_id: row
        for row in db.query(ExpiryRegisterEntry).filter(
            ExpiryRegisterEntry.store_id == store_id
        )
    }
    names = dict(
        db.query(Medicine.id, Medicine.brand_name).filter(
            Medicine.id.in_([b.medicine_id for b in batches])
        )
    )

    added = updated = 0
    for batch in batches:
        entry = existing.get(batch.id)
        if entry is not None:
            # An entry already acted on is history; leave it be.
            if entry.status in CLOSED_STATUSES:
                continue
            if entry.qty_units != batch.qty_available_units:
                entry.qty_units = batch.qty_available_units
                entry.mrp_value = quantize_money(batch.unit_mrp * batch.qty_available_units)
                entry.cost_value = (
                    quantize_money(batch.unit_cost * batch.qty_available_units)
                    if batch.unit_cost is not None
                    else None
                )
                entry.updated_by_id = actor_id
                updated += 1
            continue

        db.add(
            ExpiryRegisterEntry(
                store_id=store_id,
                medicine_id=batch.medicine_id,
                batch_id=batch.id,
                medicine_name_snapshot=names.get(batch.medicine_id, "—"),
                batch_no=batch.batch_no,
                expiry_date=batch.expiry_date,
                qty_units=batch.qty_available_units,
                mrp_value=quantize_money(batch.unit_mrp * batch.qty_available_units),
                cost_value=(
                    quantize_money(batch.unit_cost * batch.qty_available_units)
                    if batch.unit_cost is not None
                    else None
                ),
                source=ExpirySource.EXPIRED_STOCK.value,
                status=ExpiryStatus.IDENTIFIED.value,
                supplier_id=batch.supplier_id,
                created_by_id=actor_id,
                updated_by_id=actor_id,
            )
        )
        added += 1

    db.flush()
    return {"added": added, "updated": updated, "scanned": len(batches)}


def _assert_open(entry: ExpiryRegisterEntry) -> None:
    if entry.status in CLOSED_STATUSES:
        raise ConflictError(
            f"Batch {entry.batch_no} was already {entry.status.replace('_', ' ').lower()}.",
            code="EXPIRY_ALREADY_ACTIONED",
        )


def write_off(
    db: Session, *, entry: ExpiryRegisterEntry, user: User, notes: str | None = None
) -> ExpiryRegisterEntry:
    """Book the loss: take the units off the shelf through the ledger."""
    _assert_open(entry)

    batch = db.get(Batch, entry.batch_id)
    if batch is None:
        raise ExpiryError("The batch behind this entry no longer exists.", status_code=404)

    if batch.qty_available_units > ZERO:
        inventory.post_movement(
            db,
            batch=batch,
            movement_type=MovementType.EXPIRY_WRITE_OFF,
            qty_units_delta=-batch.qty_available_units,
            actor_id=user.id,
            reference_type=ReferenceType.EXPIRY,
            reference_id=entry.id,
            reason=f"Expired {entry.expiry_date} — written off",
        )
    # Quarantine it as well, so nothing can be adjusted back onto the shelf
    # by accident.
    batch.is_blocked = True
    batch.block_reason = f"Expired {entry.expiry_date}, written off"

    entry.status = ExpiryStatus.WRITTEN_OFF.value
    entry.action_date = business_date()
    entry.actioned_by_id = user.id
    entry.actioned_by_name = user.full_name
    if notes:
        entry.notes = notes
    entry.updated_by_id = user.id
    db.flush()
    return entry


def return_to_supplier(
    db: Session,
    *,
    entry: ExpiryRegisterEntry,
    supplier_id: UUID,
    debit_note_ref: str,
    user: User,
    credit_expected: Decimal | None = None,
    notes: str | None = None,
) -> ExpiryRegisterEntry:
    """Send the goods back against a debit note.

    The stock leaves the same way as a write-off — it is gone from the shop
    either way. What differs is that money is expected back, which is why
    the supplier and the debit-note reference are required.
    """
    _assert_open(entry)

    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.store_id != entry.store_id:
        raise ExpiryError("Supplier not found.", status_code=404, field="supplier_id")

    batch = db.get(Batch, entry.batch_id)
    if batch is None:
        raise ExpiryError("The batch behind this entry no longer exists.", status_code=404)

    if batch.qty_available_units > ZERO:
        inventory.post_movement(
            db,
            batch=batch,
            movement_type=MovementType.PURCHASE_RETURN,
            qty_units_delta=-batch.qty_available_units,
            actor_id=user.id,
            reference_type=ReferenceType.EXPIRY,
            reference_id=entry.id,
            reason=f"Expired stock returned to {supplier.name} on {debit_note_ref}",
        )
        batch.qty_received_units = inventory.quantize_qty(
            max(ZERO, batch.qty_received_units - batch.qty_available_units)
        )
    batch.is_blocked = True
    batch.block_reason = f"Expired {entry.expiry_date}, returned to supplier"

    entry.status = ExpiryStatus.RETURNED_TO_SUPPLIER.value
    entry.supplier_id = supplier.id
    entry.debit_note_ref = debit_note_ref
    entry.credit_received_amount = (
        quantize_money(credit_expected) if credit_expected is not None else None
    )
    entry.action_date = business_date()
    entry.actioned_by_id = user.id
    entry.actioned_by_name = user.full_name
    if notes:
        entry.notes = notes
    entry.updated_by_id = user.id
    db.flush()
    return entry


def record_credit(
    db: Session, *, entry: ExpiryRegisterEntry, amount: Decimal, user: User
) -> ExpiryRegisterEntry:
    """The supplier's credit note arrived — close the loop."""
    if entry.status != ExpiryStatus.RETURNED_TO_SUPPLIER.value:
        raise ConflictError(
            "Credit can only be recorded against stock that went back to a supplier.",
            code="EXPIRY_NOT_RETURNED",
        )

    entry.credit_received_amount = quantize_money(amount)
    entry.status = ExpiryStatus.CREDIT_RECEIVED.value
    entry.action_date = business_date()
    entry.actioned_by_id = user.id
    entry.actioned_by_name = user.full_name
    entry.updated_by_id = user.id
    db.flush()
    return entry
