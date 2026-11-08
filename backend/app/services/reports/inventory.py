"""Movement and money tied up in stock.

Two of these reports are about *time* — what sold, what did not — and two are
about *value at this moment*. The distinction matters when reading them:

* fast- and slow-moving are measured over the requested window;
* the valuation and the expiry ageing are taken as at today, because stock
  has no history in the batch table. Rebuilding the shelf as it stood on a
  past date means replaying the whole ledger, which is a different and much
  slower report; until it exists, `meta.as_at` says plainly which date the
  figures belong to rather than letting a date filter appear to apply.

**Every quantity x price product is computed in Python.** On SQLite these
columns are scaled integers, so `qty * cost` in SQL would come back scaled
twice over (see `app/db/types.py`).
"""

from __future__ import annotations

from uuid import UUID

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.types import quantize_money
from app.models.catalog import Manufacturer, Medicine
from app.models.inventory import Batch
from app.models.sale import Sale, SaleItem
from app.services.reports import (
    Column,
    ColumnType,
    ReportTable,
    money,
    pct,
    qty,
    rate,
    sum_columns,
)
from app.services.reports.gst import LIVE_SALE_STATUSES
from app.utils.clock import business_date

ZERO = Decimal("0")

#: How the expiry register slices the shelf. Upper bound in days, inclusive;
#: `None` is everything past the last bound.
AGEING_BUCKETS: list[tuple[str, int | None]] = [
    ("Expired", 0),
    ("Within 30 days", 30),
    ("31 - 60 days", 60),
    ("61 - 90 days", 90),
    ("91 - 180 days", 180),
    ("Beyond 180 days", None),
]

#: Stock with no shelf life at all — a comb, a hot water bag. It gets its own
#: row rather than being swept into "Beyond 180 days", which would be true
#: but would read as stock that is merely slow to turn over.
NO_EXPIRY_BUCKET = "Does not expire"
AGEING_ROWS: list[str] = [label for label, _ in AGEING_BUCKETS] + [NO_EXPIRY_BUCKET]


def _units_sold(db: Session, store_id: UUID, date_from: date, date_to: date) -> dict[UUID, Decimal]:
    return {
        row.medicine_id: row.units or ZERO
        for row in db.query(
            SaleItem.medicine_id.label("medicine_id"),
            func.sum(SaleItem.qty_units).label("units"),
        )
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status.in_(LIVE_SALE_STATUSES),
        )
        .group_by(SaleItem.medicine_id)
        .all()
    }


def _sale_value(db: Session, store_id: UUID, date_from: date, date_to: date) -> dict[UUID, Decimal]:
    return {
        row.medicine_id: row.value or ZERO
        for row in db.query(
            SaleItem.medicine_id.label("medicine_id"),
            func.sum(SaleItem.net_amount).label("value"),
        )
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status.in_(LIVE_SALE_STATUSES),
        )
        .group_by(SaleItem.medicine_id)
        .all()
    }


def _stock_on_hand(db: Session, store_id: UUID) -> dict[UUID, Decimal]:
    return {
        row.medicine_id: row.units or ZERO
        for row in db.query(
            Batch.medicine_id.label("medicine_id"),
            func.sum(Batch.qty_available_units).label("units"),
        )
        .filter(Batch.store_id == store_id)
        .group_by(Batch.medicine_id)
        .all()
    }


def _medicines(db: Session, store_id: UUID, ids: set[UUID]) -> dict[UUID, tuple[str, str, str]]:
    """{id: (brand, manufacturer, pack label)}."""
    if not ids:
        return {}
    rows = (
        db.query(Medicine.id, Medicine.brand_name, Manufacturer.name, Medicine.pack_label)
        .join(Manufacturer, Medicine.manufacturer_id == Manufacturer.id)
        .filter(Medicine.store_id == store_id, Medicine.id.in_(ids))
        .all()
    )
    return {row[0]: (row[1], row[2] or "", row[3] or "") for row in rows}


# --- fast moving ---------------------------------------------------------


FAST_COLUMNS = [
    Column("medicine", "Medicine", ColumnType.TEXT),
    Column("manufacturer", "Manufacturer", ColumnType.TEXT),
    Column("pack", "Pack", ColumnType.TEXT),
    Column("units_sold", "Units sold", ColumnType.QTY),
    Column("sale_value", "Sale value", ColumnType.MONEY),
    Column("stock_units", "Stock on hand", ColumnType.QTY),
    Column("turnover_ratio", "Turnover", ColumnType.RATE),
    Column("days_of_cover", "Days of cover", ColumnType.INT),
]


def fast_moving(
    db: Session, store_id: UUID, date_from: date, date_to: date, *, top: int = 50
) -> ReportTable:
    """What actually sells, and whether there is enough of it left.

    *Turnover* is units sold in the window divided by units still on the
    shelf — how many times the current holding was cleared. *Days of cover*
    is the reverse: at this window's daily rate, how long the shelf lasts.
    A fast mover with three days of cover is the line to reorder first, and
    those two columns side by side are what makes that visible.
    """
    sold = _units_sold(db, store_id, date_from, date_to)
    values = _sale_value(db, store_id, date_from, date_to)
    stock = _stock_on_hand(db, store_id)
    names = _medicines(db, store_id, set(sold))
    days = max((date_to - date_from).days + 1, 1)

    rows = []
    for medicine_id, units in sorted(sold.items(), key=lambda kv: kv[1], reverse=True)[:top]:
        brand, manufacturer, pack = names.get(medicine_id, (f"#{medicine_id}", "", ""))
        on_hand = stock.get(medicine_id, ZERO)
        per_day = units / days
        rows.append(
            {
                "medicine": brand,
                "manufacturer": manufacturer,
                "pack": pack,
                "units_sold": qty(units),
                "sale_value": money(values.get(medicine_id)),
                "stock_units": qty(on_hand),
                "turnover_ratio": rate(units / on_hand) if on_hand > 0 else "",
                "days_of_cover": int(on_hand / per_day) if per_day > 0 else "",
            }
        )

    return ReportTable(
        key="inventory_fast_moving",
        title="Fast-moving stock",
        columns=FAST_COLUMNS,
        rows=rows,
        totals=sum_columns(rows, money_keys=["sale_value"], qty_keys=["units_sold", "stock_units"]),
        meta={"top": top, "days_in_window": days, "stock_as_at": business_date().isoformat()},
    )


# --- slow moving ---------------------------------------------------------


SLOW_COLUMNS = [
    Column("medicine", "Medicine", ColumnType.TEXT),
    Column("manufacturer", "Manufacturer", ColumnType.TEXT),
    Column("pack", "Pack", ColumnType.TEXT),
    Column("units_sold", "Units sold", ColumnType.QTY),
    Column("stock_units", "Stock on hand", ColumnType.QTY),
    Column("last_sold", "Last sold", ColumnType.DATE),
    Column("days_since_sale", "Days idle", ColumnType.INT),
    Column("cost_value", "Value at cost", ColumnType.MONEY),
    Column("mrp_value", "Value at MRP", ColumnType.MONEY),
]


def slow_moving(
    db: Session, store_id: UUID, date_from: date, date_to: date, *, threshold_units: Decimal | None
) -> ReportTable:
    """Stock that is not moving, and the money standing still inside it.

    Only lines with stock on hand appear: a medicine that sold nothing
    because there was none to sell is not a slow mover, it is a stock-out,
    and it belongs in the order book instead.
    """
    threshold = ZERO if threshold_units is None else Decimal(threshold_units)
    sold = _units_sold(db, store_id, date_from, date_to)
    stock = _stock_on_hand(db, store_id)
    idle = {
        medicine_id: units
        for medicine_id, units in stock.items()
        if units > 0 and sold.get(medicine_id, ZERO) <= threshold
    }
    names = _medicines(db, store_id, set(idle))
    last_sold = _last_sold(db, store_id, set(idle))
    values = _batch_values(db, store_id, set(idle))
    today = business_date()

    rows = []
    for medicine_id, on_hand in idle.items():
        brand, manufacturer, pack = names.get(medicine_id, (f"#{medicine_id}", "", ""))
        cost_value, mrp_value = values.get(medicine_id, (ZERO, ZERO))
        sold_on = last_sold.get(medicine_id)
        rows.append(
            {
                "medicine": brand,
                "manufacturer": manufacturer,
                "pack": pack,
                "units_sold": qty(sold.get(medicine_id)),
                "stock_units": qty(on_hand),
                "last_sold": sold_on.isoformat() if sold_on else "",
                "days_since_sale": (today - sold_on).days if sold_on else "",
                "cost_value": money(cost_value),
                "mrp_value": money(mrp_value),
            }
        )

    rows.sort(key=lambda r: Decimal(r["cost_value"]), reverse=True)

    return ReportTable(
        key="inventory_slow_moving",
        title="Slow-moving stock",
        columns=SLOW_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows,
            money_keys=["cost_value", "mrp_value"],
            qty_keys=["units_sold", "stock_units"],
        ),
        meta={
            "threshold_units": qty(threshold),
            "stock_as_at": today.isoformat(),
            "medicine_count": len(rows),
        },
    )


def _last_sold(db: Session, store_id: UUID, ids: set[UUID]) -> dict[UUID, date]:
    """The last time each medicine sold — over all time, not just the window."""
    if not ids:
        return {}
    rows = (
        db.query(SaleItem.medicine_id, func.max(Sale.invoice_date))
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(
            Sale.store_id == store_id,
            Sale.status.in_(LIVE_SALE_STATUSES),
            SaleItem.medicine_id.in_(ids),
        )
        .group_by(SaleItem.medicine_id)
        .all()
    )
    return {row[0]: row[1] for row in rows if row[1] is not None}


def _batch_values(
    db: Session, store_id: UUID, ids: set[UUID]
) -> dict[UUID, tuple[Decimal, Decimal]]:
    """{medicine_id: (value at cost, value at MRP)} over batches in stock."""
    if not ids:
        return {}
    totals: dict[UUID, tuple[Decimal, Decimal]] = {}
    batches = (
        db.query(Batch)
        .filter(Batch.store_id == store_id, Batch.medicine_id.in_(ids))
        .all()
    )
    for batch in batches:
        if batch.qty_available_units <= 0:
            continue
        cost, mrp = totals.get(batch.medicine_id, (ZERO, ZERO))
        totals[batch.medicine_id] = (
            cost + batch.qty_available_units * (batch.unit_cost or ZERO),
            mrp + batch.qty_available_units * batch.unit_mrp,
        )
    return totals


# --- valuation -----------------------------------------------------------


VALUATION_COLUMNS = [
    Column("medicine", "Medicine", ColumnType.TEXT),
    Column("batch_no", "Batch", ColumnType.TEXT),
    Column("expiry_date", "Expiry", ColumnType.DATE),
    Column("stock_units", "Units", ColumnType.QTY),
    Column("unit_cost", "Cost / unit", ColumnType.MONEY),
    Column("cost_value", "Value at cost", ColumnType.MONEY),
    Column("unit_mrp", "MRP / unit", ColumnType.MONEY),
    Column("mrp_value", "Value at MRP", ColumnType.MONEY),
    Column("margin_value", "Margin", ColumnType.MONEY),
    Column("margin_pct", "Margin %", ColumnType.RATE),
]


def valuation(db: Session, store_id: UUID) -> ReportTable:
    """Batch-wise stock at cost and at MRP, as at today.

    A batch received as opening stock has no cost — nobody billed the shop
    for it. Its cost value is zero rather than guessed from MRP, and the
    count of such batches is reported so the total is read for what it is.
    """
    batches = (
        db.query(Batch, Medicine.brand_name)
        .join(Medicine, Batch.medicine_id == Medicine.id)
        .filter(Batch.store_id == store_id, Batch.qty_available_units > 0)
        .order_by(Medicine.brand_name, Batch.expiry_date)
        .all()
    )

    rows = []
    without_cost = 0
    for batch, brand in batches:
        if batch.unit_cost is None:
            without_cost += 1
        cost_value = quantize_money(batch.qty_available_units * (batch.unit_cost or ZERO))
        mrp_value = quantize_money(batch.qty_available_units * batch.unit_mrp)
        rows.append(
            {
                "medicine": brand,
                "batch_no": batch.batch_no,
                "expiry_date": batch.expiry_date.isoformat() if batch.has_expiry else "",
                "stock_units": qty(batch.qty_available_units),
                "unit_cost": money(batch.unit_cost) if batch.unit_cost is not None else "",
                "cost_value": money(cost_value),
                "unit_mrp": money(batch.unit_mrp),
                "mrp_value": money(mrp_value),
                "margin_value": money(mrp_value - cost_value),
                "margin_pct": pct(mrp_value - cost_value, mrp_value),
            }
        )

    totals = sum_columns(
        rows,
        money_keys=["cost_value", "mrp_value", "margin_value"],
        qty_keys=["stock_units"],
    )
    totals["margin_pct"] = pct(totals["margin_value"], totals["mrp_value"])

    return ReportTable(
        key="inventory_valuation",
        title="Stock valuation",
        columns=VALUATION_COLUMNS,
        rows=rows,
        totals=totals,
        meta={
            "as_at": business_date().isoformat(),
            "batch_count": len(rows),
            "batches_without_cost": without_cost,
        },
    )


# --- expiry ageing -------------------------------------------------------


AGEING_COLUMNS = [
    Column("bucket", "Expires", ColumnType.TEXT),
    Column("batch_count", "Batches", ColumnType.INT),
    Column("medicine_count", "Medicines", ColumnType.INT),
    Column("stock_units", "Units", ColumnType.QTY),
    Column("cost_value", "Value at cost", ColumnType.MONEY),
    Column("mrp_value", "Value at MRP", ColumnType.MONEY),
    Column("share_pct", "Share of stock", ColumnType.RATE),
]


def expiry_ageing(db: Session, store_id: UUID) -> ReportTable:
    """How much of the shelf is running out of time, and what it is worth.

    Every bucket is listed even when empty: a row of zeroes is information —
    it says nothing expires in that window — and a missing row reads as an
    oversight.
    """
    today = business_date()
    batches = (
        db.query(Batch)
        .filter(Batch.store_id == store_id, Batch.qty_available_units > 0)
        .all()
    )

    buckets: dict[str, dict] = {
        label: {"batches": 0, "medicines": set(), "units": ZERO, "cost": ZERO, "mrp": ZERO}
        for label in AGEING_ROWS
    }

    for batch in batches:
        if batch.has_expiry:
            label = _bucket_for((batch.expiry_date - today).days)
        else:
            label = NO_EXPIRY_BUCKET
        bucket = buckets[label]
        bucket["batches"] += 1
        bucket["medicines"].add(batch.medicine_id)
        bucket["units"] += batch.qty_available_units
        bucket["cost"] += batch.qty_available_units * (batch.unit_cost or ZERO)
        bucket["mrp"] += batch.qty_available_units * batch.unit_mrp

    total_mrp = sum((bucket["mrp"] for bucket in buckets.values()), ZERO)

    rows = [
        {
            "bucket": label,
            "batch_count": buckets[label]["batches"],
            "medicine_count": len(buckets[label]["medicines"]),
            "stock_units": qty(buckets[label]["units"]),
            "cost_value": money(buckets[label]["cost"]),
            "mrp_value": money(buckets[label]["mrp"]),
            "share_pct": pct(buckets[label]["mrp"], total_mrp),
        }
        for label in AGEING_ROWS
    ]

    totals = sum_columns(
        rows,
        money_keys=["cost_value", "mrp_value"],
        int_keys=["batch_count"],
        qty_keys=["stock_units"],
    )
    # Counted across the whole shelf, not summed down the column: one
    # medicine with two batches in two buckets is still one medicine.
    totals["medicine_count"] = len({batch.medicine_id for batch in batches})
    totals["share_pct"] = "100.00" if total_mrp > 0 else "0.00"

    return ReportTable(
        key="inventory_expiry_ageing",
        title="Expiry ageing",
        columns=AGEING_COLUMNS,
        rows=rows,
        totals=totals,
        meta={"as_at": today.isoformat()},
    )


def _bucket_for(days: int) -> str:
    """Days to expiry -> bucket label. Today counts as still sellable."""
    if days < 0:
        return AGEING_BUCKETS[0][0]
    for label, upper in AGEING_BUCKETS[1:]:
        if upper is None or days <= upper:
            return label
    return AGEING_BUCKETS[-1][0]
