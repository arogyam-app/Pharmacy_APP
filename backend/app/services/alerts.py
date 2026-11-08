"""What the owner needs to be told about, without going looking.

Five buckets, all read-only and all computed from stock as it stands
(SPEC §3.8):

* **expired** — past its date with units still on hand. Money already lost;
  the only question is whether the supplier will take it back.
* **near expiry** — inside the warning window. Still sellable, and the
  window exists so it *gets* sold.
* **low stock** — at or under the reorder level, medicine by medicine.
* **out of stock** — nothing sellable, but the shop still lists it.
* **order book** — open reorder suggestions (see services/order_book.py).

Expired and blocked stock is excluded from "available" everywhere else in
the app; here it is the point, so these queries look at batches directly
rather than going through the FEFO helpers.
"""

from uuid import UUID

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.types import quantize_money, quantize_qty
from app.models.catalog import Medicine
from app.models.enums import OrderBookStatus
from app.models.inventory import Batch
from app.models.stockwatch import OrderBookEntry
from app.models.store import StoreSettings
from app.utils.clock import business_date

ZERO = Decimal("0.000")


def _held_batches(db: Session, store_id: UUID, item_type: str | None = None):
    """Batches with units on hand, whatever their state.

    `item_type` scopes the query to medicines or to general goods, so the two
    sides of the shop each get their own alert list. Omitted, it covers both.
    """
    query = db.query(Batch).filter(
        Batch.store_id == store_id, Batch.qty_available_units > ZERO
    )
    if item_type is not None:
        query = query.join(Medicine, Batch.medicine_id == Medicine.id).filter(
            Medicine.item_type == item_type
        )
    return query


def expired_batches(
    db: Session, store_id: UUID, item_type: str | None = None
) -> list[Batch]:
    today = business_date()
    return (
        _held_batches(db, store_id, item_type)
        # Stock with no shelf life carries a sentinel far in the future, so
        # this never picks up a comb.
        .filter(Batch.expiry_date < today)
        .order_by(Batch.expiry_date)
        .all()
    )


def expiring_batches(
    db: Session, store_id: UUID, days: int, item_type: str | None = None
) -> list[Batch]:
    """Not yet expired, but inside the window — these can still be sold."""
    today = business_date()
    cutoff = today + timedelta(days=days)
    return (
        _held_batches(db, store_id, item_type)
        .filter(Batch.expiry_date >= today, Batch.expiry_date <= cutoff)
        .order_by(Batch.expiry_date)
        .all()
    )


def sellable_units_by_medicine(db: Session, store_id: UUID) -> dict[UUID, Decimal]:
    """Units that could actually be sold today, per medicine.

    Expired and quarantined stock is deliberately not counted: a shelf full
    of out-of-date boxes is not stock, and treating it as such is what stops
    the low-stock alert from firing when it should.
    """
    today = business_date()
    rows = (
        db.query(Batch.medicine_id, func.sum(Batch.qty_available_units))
        .filter(
            Batch.store_id == store_id,
            Batch.is_blocked.is_(False),
            Batch.expiry_date >= today,
            Batch.qty_available_units > ZERO,
        )
        .group_by(Batch.medicine_id)
        .all()
    )
    return {medicine_id: quantize_qty(total or ZERO) for medicine_id, total in rows}


def threshold_for(medicine: Medicine, settings: StoreSettings) -> Decimal:
    """A medicine's own reorder level, or the store-wide fallback."""
    return (
        medicine.reorder_level_units
        if medicine.reorder_level_units is not None
        else settings.low_stock_units
    )


def stock_alerts(
    db: Session, store_id: UUID, settings: StoreSettings, item_type: str | None = None
) -> tuple[list[dict], list[dict]]:
    """(low stock, out of stock) — active products only."""
    available = sellable_units_by_medicine(db, store_id)
    query = db.query(Medicine).filter(
        Medicine.store_id == store_id, Medicine.is_active.is_(True)
    )
    if item_type is not None:
        query = query.filter(Medicine.item_type == item_type)
    medicines = query.order_by(Medicine.brand_name).all()

    low, out = [], []
    for medicine in medicines:
        units = available.get(medicine.id, ZERO)
        threshold = threshold_for(medicine, settings)
        row = {
            "medicine_id": medicine.id,
            "brand_name": medicine.brand_name,
            "pack_label": medicine.pack_label,
            "units_per_pack": medicine.units_per_pack,
            "available_units": units,
            "available_packs": quantize_qty(units / Decimal(medicine.units_per_pack or 1)),
            "reorder_level_units": threshold,
            "rack_location": medicine.rack_location,
        }
        if units <= ZERO:
            out.append(row)
        elif units <= threshold:
            low.append(row)
    return low, out


def batch_row(batch: Batch, medicine_name: str | None, today) -> dict:
    """Shared shape for the expired and near-expiry lists."""
    return {
        "batch_id": batch.id,
        "medicine_id": batch.medicine_id,
        "medicine_name": medicine_name,
        "batch_no": batch.batch_no,
        "expiry_date": batch.expiry_date,
        "days_to_expiry": (batch.expiry_date - today).days,
        "qty_units": batch.qty_available_units,
        "qty_packs": quantize_qty(
            batch.qty_available_units / Decimal(batch.units_per_pack or 1)
        ),
        "mrp_value": quantize_money(batch.unit_mrp * batch.qty_available_units),
        "cost_value": (
            quantize_money(batch.unit_cost * batch.qty_available_units)
            if batch.unit_cost is not None
            else None
        ),
        "is_blocked": batch.is_blocked,
    }


def summary(
    db: Session, store_id: UUID, settings: StoreSettings, item_type: str | None = None
) -> dict:
    """The counts behind the nav badges, for one side of the shop or both."""
    low, out = stock_alerts(db, store_id, settings, item_type)
    expired = expired_batches(db, store_id, item_type)
    expiring = expiring_batches(db, store_id, settings.near_expiry_days, item_type)

    order_book_open = (
        db.query(func.count(OrderBookEntry.id))
        .filter(
            OrderBookEntry.store_id == store_id,
            OrderBookEntry.status.in_(
                [OrderBookStatus.OPEN.value, OrderBookStatus.ORDERED.value]
            ),
        )
        .scalar()
    )

    return {
        "expired": len(expired),
        "near_expiry": len(expiring),
        "low_stock": len(low),
        "out_of_stock": len(out),
        "order_book_open": order_book_open or 0,
        "near_expiry_days": settings.near_expiry_days,
        # What the expired stock is costing, at retail — the number that
        # makes the case for acting on it.
        "expired_value": quantize_money(
            sum((b.unit_mrp * b.qty_available_units for b in expired), Decimal("0"))
        ),
    }
