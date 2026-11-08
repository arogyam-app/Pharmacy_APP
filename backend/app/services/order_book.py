"""Automatic reorder suggestions.

The rule (SPEC §3.9): each medicine remembers `stock_baseline_units`, the
total on hand immediately after the last goods-inward. When stock has
fallen by `order_book_decline_pct` from that baseline — 60 % by default —
or is at or below the reorder level, an entry is raised.

Measuring against the last delivery rather than a fixed number is what
makes this work across a catalogue where one medicine sells in tens and
another in hundreds: the threshold scales with how much the shop actually
buys of each.

At most one entry per medicine is open at a time. A second would be the
same suggestion twice, so an existing row is refreshed instead.
"""

from uuid import UUID

from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.types import quantize_money, quantize_qty
from app.models.catalog import Medicine
from app.models.enums import MovementType, OrderBookStatus, OrderBookTrigger
from app.models.inventory import Batch, StockLedger
from app.models.stockwatch import OrderBookEntry
from app.models.store import StoreSettings
from app.services import alerts

ZERO = Decimal("0.000")
HUNDRED = Decimal("100")
CONSUMPTION_WINDOW_DAYS = 90

LIVE_STATUSES = (OrderBookStatus.OPEN.value, OrderBookStatus.ORDERED.value)


def open_entry_for(db: Session, store_id: UUID, medicine_id: UUID) -> OrderBookEntry | None:
    return (
        db.query(OrderBookEntry)
        .filter(
            OrderBookEntry.store_id == store_id,
            OrderBookEntry.medicine_id == medicine_id,
            OrderBookEntry.status.in_(LIVE_STATUSES),
        )
        .order_by(OrderBookEntry.id.desc())
        .first()
    )


def monthly_consumption(db: Session, store_id: UUID, medicine_id: UUID) -> Decimal:
    """Average units sold a month, over the last 90 days.

    Sales only — a write-off or a stock correction is not demand, and
    counting it would have the shop reordering to replace losses.
    """
    since = datetime.now(timezone.utc) - timedelta(days=CONSUMPTION_WINDOW_DAYS)
    sold = (
        db.query(func.sum(StockLedger.qty_units_delta))
        .filter(
            StockLedger.store_id == store_id,
            StockLedger.medicine_id == medicine_id,
            StockLedger.movement_type == MovementType.SALE.value,
            StockLedger.created_at >= since,
        )
        .scalar()
    ) or ZERO
    # SALE rows are negative; demand is the size of them.
    return quantize_qty(abs(Decimal(sold)) / Decimal(3))


def last_supplier(db: Session, store_id: UUID, medicine_id: UUID) -> tuple[UUID | None, Decimal | None]:
    """Who supplied this medicine last, and at what cost per pack."""
    batch = (
        db.query(Batch)
        .filter(
            Batch.store_id == store_id,
            Batch.medicine_id == medicine_id,
            Batch.supplier_id.isnot(None),
        )
        .order_by(Batch.received_date.desc(), Batch.id.desc())
        .first()
    )
    if batch is None:
        return None, None
    return batch.supplier_id, batch.cost_per_pack


def _round_up_to_pack(units: Decimal, units_per_pack: int) -> tuple[Decimal, Decimal]:
    """(units, packs) rounded up — distributors sell whole packs."""
    per = Decimal(units_per_pack or 1)
    packs = (Decimal(units) / per).quantize(Decimal("1"), rounding=ROUND_CEILING)
    if packs < 1:
        packs = Decimal("1")
    return quantize_qty(packs * per), quantize_qty(packs)


def evaluate(
    db: Session,
    *,
    store_id: UUID,
    medicine: Medicine,
    settings: StoreSettings,
    available_units: Decimal | None = None,
    actor_id: UUID | None = None,
) -> OrderBookEntry | None:
    """Raise or refresh an entry for one medicine, if it needs reordering.

    Returns the entry when one is in force, otherwise None.
    """
    if not medicine.is_active:
        return None

    if available_units is None:
        available_units = alerts.sellable_units_by_medicine(db, store_id).get(
            medicine.id, ZERO
        )
    available = quantize_qty(available_units)

    baseline = quantize_qty(medicine.stock_baseline_units or ZERO)
    reorder_level = alerts.threshold_for(medicine, settings)

    # Stock has declined by the configured percentage from the last delivery.
    trigger_units = (
        quantize_qty(baseline * (HUNDRED - settings.order_book_decline_pct) / HUNDRED)
        if baseline > ZERO
        else ZERO
    )
    declined = baseline > ZERO and available <= trigger_units

    reason = None
    if available <= ZERO:
        reason = OrderBookTrigger.OUT_OF_STOCK
    elif available <= reorder_level:
        reason = OrderBookTrigger.BELOW_REORDER_LEVEL
    elif declined:
        reason = OrderBookTrigger.STOCK_DECLINE_60

    existing = open_entry_for(db, store_id, medicine.id)
    if reason is None:
        # Nothing needs reordering. Returning None — rather than the entry
        # that happens to be open — is what lets `recompute_all` tell
        # "still needed" from "stock has recovered since"; any open row is
        # left untouched either way.
        return None

    # Enough to bring stock back to whichever of the two marks is higher.
    shortfall = max(baseline - available, reorder_level - available, ZERO)
    suggested_units, suggested_packs = _round_up_to_pack(
        shortfall, medicine.units_per_pack
    )

    consumption = monthly_consumption(db, store_id, medicine.id)
    days_of_cover = (
        int((available / consumption) * 30) if consumption > ZERO else None
    )
    supplier_id, cost = last_supplier(db, store_id, medicine.id)
    decline_pct = (
        quantize_money((baseline - available) / baseline * HUNDRED)
        if baseline > ZERO
        else Decimal("0.00")
    )

    entry = existing or OrderBookEntry(
        store_id=store_id,
        medicine_id=medicine.id,
        created_by_id=actor_id,
    )
    entry.trigger_reason = reason.value
    entry.baseline_units = baseline
    entry.available_units_at_trigger = available
    entry.decline_pct = decline_pct
    entry.suggested_qty_units = suggested_units
    entry.suggested_qty_packs = suggested_packs
    entry.avg_monthly_consumption_units = consumption
    entry.days_of_cover = days_of_cover
    entry.last_purchase_cost = cost
    entry.updated_by_id = actor_id
    # Only fill the supplier automatically while nobody has chosen one.
    if entry.preferred_supplier_id is None:
        entry.preferred_supplier_id = supplier_id
    if existing is None:
        entry.status = OrderBookStatus.OPEN.value
        db.add(entry)

    db.flush()
    return entry


def evaluate_medicines(
    db: Session,
    *,
    store_id: UUID,
    medicine_ids,
    settings: StoreSettings,
    actor_id: UUID | None = None,
) -> list[OrderBookEntry]:
    """Re-check a handful of medicines — called after a sale."""
    ids = list(dict.fromkeys(medicine_ids))
    if not ids:
        return []

    available = alerts.sellable_units_by_medicine(db, store_id)
    raised = []
    for medicine in db.query(Medicine).filter(Medicine.id.in_(ids)):
        entry = evaluate(
            db,
            store_id=store_id,
            medicine=medicine,
            settings=settings,
            available_units=available.get(medicine.id, ZERO),
            actor_id=actor_id,
        )
        if entry is not None:
            raised.append(entry)
    return raised


def recompute_all(
    db: Session, *, store_id: UUID, settings: StoreSettings, actor_id: UUID | None = None
) -> dict:
    """Sweep the whole catalogue. Also closes entries that no longer apply."""
    available = alerts.sellable_units_by_medicine(db, store_id)
    medicines = (
        db.query(Medicine)
        .filter(Medicine.store_id == store_id, Medicine.is_active.is_(True))
        .all()
    )

    before = {
        e.medicine_id: e
        for e in db.query(OrderBookEntry).filter(
            OrderBookEntry.store_id == store_id,
            OrderBookEntry.status == OrderBookStatus.OPEN.value,
        )
    }

    raised = refreshed = 0
    still_needed = set()
    for medicine in medicines:
        was_open = medicine.id in before
        entry = evaluate(
            db,
            store_id=store_id,
            medicine=medicine,
            settings=settings,
            available_units=available.get(medicine.id, ZERO),
            actor_id=actor_id,
        )
        if entry is None:
            continue
        still_needed.add(medicine.id)
        if was_open:
            refreshed += 1
        else:
            raised += 1

    # An OPEN entry for a medicine that has since been restocked by hand is
    # noise; close it. An ORDERED one is left alone — goods are on the way.
    closed = 0
    for medicine_id, entry in before.items():
        if medicine_id not in still_needed:
            entry.status = OrderBookStatus.CANCELLED.value
            entry.closed_at = datetime.now(timezone.utc)
            entry.notes = "Closed automatically: stock recovered."
            entry.updated_by_id = actor_id
            closed += 1

    db.flush()
    return {"raised": raised, "refreshed": refreshed, "closed": closed}


def close_for_purchase(
    db: Session,
    *,
    store_id: UUID,
    medicine_ids,
    purchase_invoice_id: UUID,
    actor_id: UUID | None = None,
) -> int:
    """Receiving goods answers the suggestion — close the entries it covers."""
    ids = list(dict.fromkeys(medicine_ids))
    if not ids:
        return 0

    entries = (
        db.query(OrderBookEntry)
        .filter(
            OrderBookEntry.store_id == store_id,
            OrderBookEntry.medicine_id.in_(ids),
            OrderBookEntry.status.in_(LIVE_STATUSES),
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for entry in entries:
        entry.status = OrderBookStatus.RECEIVED.value
        entry.purchase_invoice_id = purchase_invoice_id
        entry.closed_at = now
        entry.updated_by_id = actor_id
    db.flush()
    return len(entries)
