"""Where the money came from: margin, the people behind it, and the paper trail.

Profitability is the report with the most ways to be quietly wrong, so two
choices are made explicitly here:

* **Revenue is the taxable value, not the invoice value.** GST collected at
  the counter is the government's money passing through the till; counting
  it as income would inflate every margin by the tax rate.
* **Cost is the snapshot taken at the moment of sale** (`unit_cost_snapshot`),
  never today's cost on the batch. A consignment bought cheaper next month
  must not retrospectively improve last month's margin.

Returns are netted off both sides — revenue *and* cost — using the cost that
was snapshotted onto the line the goods originally left on.
"""

from __future__ import annotations

from uuid import UUID

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.catalog import Manufacturer, Medicine
from app.models.enums import AuditAction, CreditNoteStatus, ReturnStatus
from app.models.party import Customer, Doctor
from app.models.returns import CreditNote, SaleReturn, SaleReturnItem
from app.models.sale import Sale, SaleItem
from app.models.user import User
from app.services.reports import (
    Column,
    ColumnType,
    ReportTable,
    money,
    pct,
    qty,
    sum_columns,
)
from app.services.reports.gst import LIVE_SALE_STATUSES
from app.utils.clock import business_date, business_day_bounds, utc_iso

ZERO = Decimal("0")

GROUP_BY_MEDICINE = "medicine"
GROUP_BY_MANUFACTURER = "manufacturer"
GROUP_BY_MONTH = "month"
GROUP_BY_CHOICES = (GROUP_BY_MEDICINE, GROUP_BY_MANUFACTURER, GROUP_BY_MONTH)

GROUP_LABELS = {
    GROUP_BY_MEDICINE: "Medicine",
    GROUP_BY_MANUFACTURER: "Manufacturer",
    GROUP_BY_MONTH: "Month",
}


def profitability_columns(group_by: str) -> list[Column]:
    return [
        Column("group", GROUP_LABELS[group_by], ColumnType.TEXT),
        Column("units_sold", "Units sold", ColumnType.QTY),
        Column("sale_value", "Sale value", ColumnType.MONEY),
        Column("revenue", "Revenue (ex-GST)", ColumnType.MONEY),
        Column("cost_value", "Cost", ColumnType.MONEY),
        Column("margin_value", "Margin", ColumnType.MONEY),
        Column("margin_pct", "Margin %", ColumnType.RATE),
    ]


def profitability(
    db: Session, store_id: UUID, date_from: date, date_to: date, *, group_by: str
) -> ReportTable:
    """Revenue less cost of goods sold, grouped three ways.

    The quantity x cost products are done in Python: multiplying two scaled
    decimal columns in SQL is wrong on SQLite (`app/db/types.py`).
    """
    sold = (
        db.query(
            SaleItem.medicine_id.label("medicine_id"),
            Medicine.brand_name.label("brand_name"),
            Manufacturer.name.label("manufacturer"),
            Sale.invoice_date.label("invoice_date"),
            SaleItem.qty_units.label("qty_units"),
            SaleItem.unit_cost_snapshot.label("unit_cost"),
            SaleItem.taxable_value.label("taxable_value"),
            SaleItem.net_amount.label("net_amount"),
        )
        .join(Sale, SaleItem.sale_id == Sale.id)
        .join(Medicine, SaleItem.medicine_id == Medicine.id)
        .join(Manufacturer, Medicine.manufacturer_id == Manufacturer.id)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status.in_(LIVE_SALE_STATUSES),
        )
        .all()
    )

    returned = (
        db.query(
            SaleItem.medicine_id.label("medicine_id"),
            Medicine.brand_name.label("brand_name"),
            Manufacturer.name.label("manufacturer"),
            SaleReturn.return_date.label("invoice_date"),
            SaleReturnItem.qty_units.label("qty_units"),
            SaleItem.unit_cost_snapshot.label("unit_cost"),
            SaleReturnItem.taxable_value.label("taxable_value"),
            SaleReturnItem.refund_amount.label("net_amount"),
        )
        .join(SaleReturn, SaleReturnItem.sale_return_id == SaleReturn.id)
        .join(SaleItem, SaleReturnItem.sale_item_id == SaleItem.id)
        .join(Medicine, SaleReturnItem.medicine_id == Medicine.id)
        .join(Manufacturer, Medicine.manufacturer_id == Manufacturer.id)
        .filter(
            SaleReturn.store_id == store_id,
            SaleReturn.return_date >= date_from,
            SaleReturn.return_date <= date_to,
            SaleReturn.status == ReturnStatus.COMPLETED.value,
        )
        .all()
    )

    buckets: dict[str, dict[str, Decimal]] = {}

    def add(row, sign: int) -> None:
        key = _group_key(row, group_by)
        bucket = buckets.setdefault(
            key, {"units": ZERO, "sale": ZERO, "revenue": ZERO, "cost": ZERO}
        )
        bucket["units"] += sign * row.qty_units
        bucket["sale"] += sign * (row.net_amount or ZERO)
        bucket["revenue"] += sign * (row.taxable_value or ZERO)
        bucket["cost"] += sign * row.qty_units * (row.unit_cost or ZERO)

    for row in sold:
        add(row, 1)
    for row in returned:
        add(row, -1)

    rows = []
    for key, bucket in buckets.items():
        margin = bucket["revenue"] - bucket["cost"]
        rows.append(
            {
                "group": key,
                "units_sold": qty(bucket["units"]),
                "sale_value": money(bucket["sale"]),
                "revenue": money(bucket["revenue"]),
                "cost_value": money(bucket["cost"]),
                "margin_value": money(margin),
                "margin_pct": pct(margin, bucket["revenue"]),
            }
        )

    if group_by == GROUP_BY_MONTH:
        rows.sort(key=lambda r: r["group"])
    else:
        rows.sort(key=lambda r: Decimal(r["margin_value"]), reverse=True)

    totals = sum_columns(
        rows,
        money_keys=["sale_value", "revenue", "cost_value", "margin_value"],
        qty_keys=["units_sold"],
    )
    totals["margin_pct"] = pct(totals["margin_value"], totals["revenue"])

    # An opening-stock line has no cost, so its margin is the whole revenue.
    # Saying how many such lines there were stops the total being read as a
    # real margin when the shop is still selling stock it never bought in.
    lines_without_cost = sum(1 for row in sold if row.unit_cost is None)

    return ReportTable(
        key="profitability",
        title=f"Profitability by {group_by}",
        columns=profitability_columns(group_by),
        rows=rows,
        totals=totals,
        meta={"group_by": group_by, "lines_without_cost": lines_without_cost},
    )


def _group_key(row, group_by: str) -> str:
    if group_by == GROUP_BY_MANUFACTURER:
        return row.manufacturer or "Unknown"
    if group_by == GROUP_BY_MONTH:
        return row.invoice_date.strftime("%Y-%m")
    return row.brand_name


# --- customers and doctors ----------------------------------------------


TOP_CUSTOMER_COLUMNS = [
    Column("customer", "Customer", ColumnType.TEXT),
    Column("mobile", "Mobile", ColumnType.TEXT),
    Column("invoice_count", "Invoices", ColumnType.INT),
    Column("net_amount", "Billed", ColumnType.MONEY),
    Column("average_invoice", "Average invoice", ColumnType.MONEY),
    Column("discount_amount", "Discount given", ColumnType.MONEY),
    Column("last_visit", "Last visit", ColumnType.DATE),
]


def top_customers(
    db: Session, store_id: UUID, date_from: date, date_to: date, *, top: int = 50
) -> ReportTable:
    """Who spends the most. Walk-ins are grouped by the mobile on the bill.

    A sale with neither a customer record nor a mobile cannot be attributed
    to anybody, so it is left out rather than lumped into a meaningless
    "walk-in" row that would top every list.
    """
    rows_raw = (
        db.query(
            func.coalesce(Customer.name, Sale.customer_name_snapshot).label("name"),
            func.coalesce(Customer.mobile, Sale.customer_mobile_snapshot).label("mobile"),
            func.count(Sale.id).label("invoice_count"),
            func.sum(Sale.net_amount).label("net_amount"),
            func.sum(Sale.discount_amount).label("discount_amount"),
            func.max(Sale.invoice_date).label("last_visit"),
        )
        .outerjoin(Customer, Sale.customer_id == Customer.id)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status.in_(LIVE_SALE_STATUSES),
            or_(Sale.customer_id.isnot(None), Sale.customer_mobile_snapshot.isnot(None)),
        )
        .group_by(
            func.coalesce(Customer.name, Sale.customer_name_snapshot),
            func.coalesce(Customer.mobile, Sale.customer_mobile_snapshot),
        )
        .order_by(func.sum(Sale.net_amount).desc())
        .limit(top)
        .all()
    )

    rows = [
        {
            "customer": row.name or "—",
            "mobile": row.mobile or "",
            "invoice_count": int(row.invoice_count or 0),
            "net_amount": money(row.net_amount),
            "average_invoice": money((row.net_amount or ZERO) / (row.invoice_count or 1)),
            "discount_amount": money(row.discount_amount),
            "last_visit": row.last_visit.isoformat() if row.last_visit else "",
        }
        for row in rows_raw
    ]

    return ReportTable(
        key="top_customers",
        title="Top customers",
        columns=TOP_CUSTOMER_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows,
            money_keys=["net_amount", "discount_amount"],
            int_keys=["invoice_count"],
        ),
        meta={"top": top},
    )


TOP_DOCTOR_COLUMNS = [
    Column("doctor", "Doctor", ColumnType.TEXT),
    Column("registration_no", "Registration", ColumnType.TEXT),
    Column("prescription_count", "Prescriptions", ColumnType.INT),
    Column("patient_count", "Patients", ColumnType.INT),
    Column("net_amount", "Value billed", ColumnType.MONEY),
    Column("last_seen", "Last prescription", ColumnType.DATE),
]


def top_doctors(
    db: Session, store_id: UUID, date_from: date, date_to: date, *, top: int = 50
) -> ReportTable:
    """Which prescribers bring the counter its business."""
    rows_raw = (
        db.query(
            Doctor.name.label("name"),
            Doctor.registration_no.label("registration_no"),
            func.count(Sale.id).label("prescription_count"),
            func.count(func.distinct(Sale.customer_mobile_snapshot)).label("patient_count"),
            func.sum(Sale.net_amount).label("net_amount"),
            func.max(Sale.invoice_date).label("last_seen"),
        )
        .join(Doctor, Sale.doctor_id == Doctor.id)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status.in_(LIVE_SALE_STATUSES),
        )
        .group_by(Doctor.id, Doctor.name, Doctor.registration_no)
        .order_by(func.sum(Sale.net_amount).desc())
        .limit(top)
        .all()
    )

    rows = [
        {
            "doctor": row.name,
            "registration_no": row.registration_no or "",
            "prescription_count": int(row.prescription_count or 0),
            "patient_count": int(row.patient_count or 0),
            "net_amount": money(row.net_amount),
            "last_seen": row.last_seen.isoformat() if row.last_seen else "",
        }
        for row in rows_raw
    ]

    return ReportTable(
        key="top_doctors",
        title="Top doctors",
        columns=TOP_DOCTOR_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows,
            money_keys=["net_amount"],
            int_keys=["prescription_count", "patient_count"],
        ),
        meta={"top": top},
    )


# --- credit notes --------------------------------------------------------


CREDIT_NOTE_COLUMNS = [
    Column("credit_note_no", "Credit note", ColumnType.TEXT),
    Column("issue_date", "Issued", ColumnType.DATE),
    Column("customer", "Customer", ColumnType.TEXT),
    Column("mobile", "Mobile", ColumnType.TEXT),
    Column("amount_issued", "Issued", ColumnType.MONEY),
    Column("amount_used", "Redeemed", ColumnType.MONEY),
    Column("balance_amount", "Outstanding", ColumnType.MONEY),
    Column("expires_on", "Expires", ColumnType.DATE),
    Column("days_to_expiry", "Days left", ColumnType.INT),
    Column("status", "Status", ColumnType.TEXT),
]


def credit_note_ledger(
    db: Session, store_id: UUID, date_from: date, date_to: date
) -> ReportTable:
    """Every note raised in the window, and what is still owed on it.

    Outstanding credit is a liability — the shop owes those customers goods —
    so the summary separates what is still live from what is about to lapse.
    """
    today = business_date()
    notes = (
        db.query(CreditNote, Customer)
        .join(Customer, CreditNote.customer_id == Customer.id)
        .filter(
            CreditNote.store_id == store_id,
            CreditNote.issue_date >= date_from,
            CreditNote.issue_date <= date_to,
        )
        .order_by(CreditNote.issue_date, CreditNote.credit_note_no)
        .all()
    )

    rows = []
    expiring_soon = ZERO
    outstanding = ZERO
    for note, customer in notes:
        days_left = (note.expires_on - today).days
        live = note.status in (
            CreditNoteStatus.OPEN.value,
            CreditNoteStatus.PARTIALLY_USED.value,
        )
        if live:
            outstanding += note.balance_amount
            if 0 <= days_left <= 30:
                expiring_soon += note.balance_amount
        rows.append(
            {
                "credit_note_no": note.credit_note_no,
                "issue_date": note.issue_date.isoformat(),
                "customer": customer.name or "—",
                "mobile": customer.mobile,
                "amount_issued": money(note.amount_issued),
                "amount_used": money(note.amount_used),
                "balance_amount": money(note.balance_amount),
                "expires_on": note.expires_on.isoformat(),
                "days_to_expiry": days_left,
                "status": note.status,
            }
        )

    return ReportTable(
        key="credit_note_ledger",
        title="Credit-note ledger",
        columns=CREDIT_NOTE_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows, money_keys=["amount_issued", "amount_used", "balance_amount"]
        ),
        meta={
            "note_count": len(rows),
            "outstanding": money(outstanding),
            "expiring_within_30_days": money(expiring_soon),
        },
    )


# --- audit ---------------------------------------------------------------


AUDIT_COLUMNS = [
    Column("at", "When", ColumnType.DATETIME),
    Column("user", "User", ColumnType.TEXT),
    Column("action", "Action", ColumnType.TEXT),
    Column("entity_type", "Entity", ColumnType.TEXT),
    Column("entity_id", "Ref", ColumnType.INT),
    Column("reason", "Reason", ColumnType.TEXT),
    Column("detail", "Detail", ColumnType.TEXT),
    Column("ip_address", "IP", ColumnType.TEXT),
]

#: The actions an owner reviews: money undone, and discretion exercised.
REVIEWABLE_ACTIONS = (
    AuditAction.VOID.value,
    AuditAction.APPROVE.value,
    AuditAction.REJECT.value,
    AuditAction.OVERRIDE.value,
    AuditAction.DELETE.value,
)


def audit_report(db: Session, store_id: UUID, date_from: date, date_to: date) -> ReportTable:
    """Voids, approvals, rejections and discount overrides.

    `created_at` is a UTC instant and the window is a run of business days,
    so the two are bridged by `business_day_bounds` rather than by comparing
    the timestamp to a bare date — which would silently drop everything done
    before 05:30 IST on the closing day (CLAUDE.md rule 9).
    """
    start, end = business_day_bounds(date_from, date_to)

    entries = (
        db.query(AuditLog, User.full_name)
        .outerjoin(User, AuditLog.user_id == User.id)
        .filter(
            AuditLog.store_id == store_id,
            AuditLog.created_at >= start,
            AuditLog.created_at < end,
            AuditLog.action.in_(REVIEWABLE_ACTIONS),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .all()
    )

    rows = [
        {
            "at": utc_iso(entry.created_at),
            "user": name or "—",
            "action": entry.action,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "reason": entry.reason or "",
            "detail": _audit_detail(entry),
            "ip_address": entry.ip_address or "",
        }
        for entry, name in entries
    ]

    by_action: dict[str, int] = {}
    for row in rows:
        by_action[row["action"]] = by_action.get(row["action"], 0) + 1

    return ReportTable(
        key="audit_report",
        title="Void & audit report",
        columns=AUDIT_COLUMNS,
        rows=rows,
        meta={"entry_count": len(rows), "by_action": by_action},
    )


def _audit_detail(entry: AuditLog) -> str:
    """The one or two fields worth reading out of the after-image.

    The full before/after JSON is on the audit row for anyone who needs it;
    a report column that dumped it would be unreadable and would leak more
    than the question asked.
    """
    if not entry.after_json:
        return ""
    try:
        after = json.loads(entry.after_json)
    except (TypeError, ValueError):
        return ""
    if not isinstance(after, dict):
        return ""

    interesting = ("invoice_no", "credit_note_no", "return_no", "discount_pct", "net_amount",
                   "status", "amount")
    parts = [f"{key}={after[key]}" for key in interesting if after.get(key) not in (None, "")]
    return ", ".join(parts)
