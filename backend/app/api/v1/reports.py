"""Reports — admin only, date-ranged, and downloadable.

Every route here is the same three lines: build a `ReportTable` in the
service layer, hand it to `render`, done. `?format=` is implemented once, so
a report added next month is a service function and one route, and it can be
exported to CSV and XLSX without anybody writing an exporter.

Cost, margin and supplier prices run through these reports, which is why the
whole router is behind `AdminUser` — a sales executive gets a 403 from the
route, not a hidden menu item (CLAUDE.md rule 4).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query

from app.core.deps import AdminUser, DbSession
from app.schemas.report import ReportFormat, ReportOut, ReportRange
from app.services.reports import NUMERIC_TYPES, ReportTable
from app.services.reports import business, gst
from app.services.reports import inventory as inventory_reports
from app.services.reports import sales as sales_reports
from app.utils.csv_export import csv_response
from app.utils.spreadsheet import xlsx_response, xlsx_table

router = APIRouter()


def render(table: ReportTable, date_range, fmt: ReportFormat):
    """One report, as JSON, a CSV download or a spreadsheet."""
    if fmt == "csv":
        return csv_response(
            filename=f"{date_range.slug(table.key.replace('_', '-'))}.csv",
            header=[column.label for column in table.columns],
            rows=_csv_rows(table),
        )

    if fmt == "xlsx":
        content = xlsx_table(
            [(c.key, c.label, c.type) for c in table.columns],
            table.rows,
            title=table.title,
            subtitle=date_range.label,
            totals=table.totals,
            numeric_types=NUMERIC_TYPES,
        )
        return xlsx_response(content, f"{date_range.slug(table.key.replace('_', '-'))}.xlsx")

    return ReportOut(
        report=table.key,
        title=table.title,
        date_from=date_range.date_from,
        date_to=date_range.date_to,
        generated_at=datetime.now(timezone.utc).isoformat(),
        columns=[{"key": c.key, "label": c.label, "type": c.type} for c in table.columns],
        rows=table.rows,
        totals=table.totals,
        meta=table.meta,
    )


def _csv_rows(table: ReportTable):
    for row in table.rows:
        yield [row.get(column.key) for column in table.columns]
    if table.totals:
        # Same trick as the spreadsheet: label the row if the report's first
        # column has nothing to say about a total.
        totals = dict(table.totals)
        first = table.columns[0].key
        totals.setdefault(first, "Total")
        yield [totals.get(column.key) for column in table.columns]


FormatQuery = Query("json", description="json | csv | xlsx")


# --- GST -----------------------------------------------------------------


@router.get("/gst/outward-summary", response_model=None)
def gst_outward_summary(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Rate-wise outward supply, B2C vs B2B, net of the period's credit notes."""
    table = gst.outward_summary(db, user.store_id, date_range.date_from, date_range.date_to)
    return render(table, date_range, format)


@router.get("/gst/inward-summary", response_model=None)
def gst_inward_summary(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """The purchase register: supplier and rate-wise input tax."""
    table = gst.inward_summary(db, user.store_id, date_range.date_from, date_range.date_to)
    return render(table, date_range, format)


@router.get("/gst/net-liability", response_model=None)
def gst_net_liability(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Outward tax less input credit, per rate, per month."""
    table = gst.net_liability(db, user.store_id, date_range.date_from, date_range.date_to)
    return render(table, date_range, format)


# --- sales ---------------------------------------------------------------


@router.get("/sales/daybook", response_model=None)
def sales_daybook(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Invoice-wise takings, with the payment-mode split for the drawer."""
    table = sales_reports.daybook(db, user.store_id, date_range.date_from, date_range.date_to)
    return render(table, date_range, format)


@router.get("/sales/by-executive", response_model=None)
def sales_by_executive(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Volume, discounting and the void/return rate for each executive."""
    table = sales_reports.by_executive(db, user.store_id, date_range.date_from, date_range.date_to)
    return render(table, date_range, format)


# --- inventory -----------------------------------------------------------


@router.get("/inventory/fast-moving", response_model=None)
def inventory_fast_moving(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    top: int = Query(50, ge=1, le=500),
    format: ReportFormat = FormatQuery,
):
    """The best sellers of the window, with turnover and days of cover."""
    table = inventory_reports.fast_moving(
        db, user.store_id, date_range.date_from, date_range.date_to, top=top
    )
    return render(table, date_range, format)


@router.get("/inventory/slow-moving", response_model=None)
def inventory_slow_moving(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    threshold_units: Decimal | None = Query(None, ge=0),
    format: ReportFormat = FormatQuery,
):
    """Stock that did not move past the threshold, and the money inside it."""
    table = inventory_reports.slow_moving(
        db, user.store_id, date_range.date_from, date_range.date_to,
        threshold_units=threshold_units,
    )
    return render(table, date_range, format)


@router.get("/inventory/valuation", response_model=None)
def inventory_valuation(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Batch-wise stock at cost and at MRP. As at today — see `meta.as_at`."""
    return render(inventory_reports.valuation(db, user.store_id), date_range, format)


@router.get("/inventory/expiry-ageing", response_model=None)
def inventory_expiry_ageing(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Expired / 30 / 60 / 90 / 180-day buckets, by value. As at today."""
    return render(inventory_reports.expiry_ageing(db, user.store_id), date_range, format)


# --- margin, people, credit and the paper trail --------------------------


@router.get("/profitability", response_model=None)
def profitability(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    group_by: Literal["medicine", "manufacturer", "month"] = "medicine",
    format: ReportFormat = FormatQuery,
):
    """Revenue (ex-GST) less cost of goods sold, net of returns."""
    table = business.profitability(
        db, user.store_id, date_range.date_from, date_range.date_to, group_by=group_by
    )
    return render(table, date_range, format)


@router.get("/customers/top", response_model=None)
def customers_top(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    top: int = Query(50, ge=1, le=500),
    format: ReportFormat = FormatQuery,
):
    """Customers ranked by what they were billed."""
    table = business.top_customers(
        db, user.store_id, date_range.date_from, date_range.date_to, top=top
    )
    return render(table, date_range, format)


@router.get("/doctors/top", response_model=None)
def doctors_top(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    top: int = Query(50, ge=1, le=500),
    format: ReportFormat = FormatQuery,
):
    """Prescribers ranked by the value they sent to the counter."""
    table = business.top_doctors(
        db, user.store_id, date_range.date_from, date_range.date_to, top=top
    )
    return render(table, date_range, format)


@router.get("/credit-notes/ledger", response_model=None)
def credit_notes_ledger(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Notes issued in the window: redeemed, outstanding and expiring."""
    table = business.credit_note_ledger(
        db, user.store_id, date_range.date_from, date_range.date_to
    )
    return render(table, date_range, format)


@router.get("/audit", response_model=None)
def audit(
    user: AdminUser,
    db: DbSession,
    date_range: ReportRange,
    format: ReportFormat = FormatQuery,
):
    """Voids, approvals, rejections and discount overrides."""
    table = business.audit_report(db, user.store_id, date_range.date_from, date_range.date_to)
    return render(table, date_range, format)
