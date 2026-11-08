"""What the counter did: the day-book, and each executive's numbers.

The day-book is the report the owner reads at closing time, so it is
invoice-wise rather than aggregated — the question being answered is "does
the till agree with the system", and that is settled invoice by invoice.
The payment-mode breakdown that reconciles the cash drawer rides along in
`meta`, because it is a different shape from the rows.

A voided invoice appears here where it is deliberately absent from the GST
report: the day-book is an operational record, and "this invoice was rung up
and then voided" is exactly what the person cashing up needs to see. It is
marked, and it is excluded from the money columns.
"""

from __future__ import annotations

from uuid import UUID

from datetime import date
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.enums import RefundMode, ReturnStatus, SaleStatus
from app.models.returns import SaleReturn
from app.models.sale import Payment, Sale, SaleItem
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
from app.utils.clock import utc_iso

ZERO = Decimal("0")


# --- day-book ------------------------------------------------------------


DAYBOOK_COLUMNS = [
    Column("invoice_date", "Date", ColumnType.DATE),
    Column("invoice_no", "Invoice", ColumnType.TEXT),
    Column("invoice_time", "Time", ColumnType.DATETIME),
    Column("customer", "Customer", ColumnType.TEXT),
    Column("sales_user", "Billed by", ColumnType.TEXT),
    Column("line_count", "Lines", ColumnType.INT),
    Column("gross_amount", "Gross", ColumnType.MONEY),
    Column("discount_amount", "Discount", ColumnType.MONEY),
    Column("taxable_amount", "Taxable", ColumnType.MONEY),
    Column("tax_amount", "GST", ColumnType.MONEY),
    Column("round_off", "Round off", ColumnType.MONEY),
    Column("net_amount", "Net", ColumnType.MONEY),
    Column("payment_modes", "Paid by", ColumnType.TEXT),
    Column("status", "Status", ColumnType.TEXT),
]


def daybook(db: Session, store_id: UUID, date_from: date, date_to: date) -> ReportTable:
    """Invoice-wise takings, with the payment-mode split for the drawer."""
    sales = (
        db.query(Sale)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status != SaleStatus.DRAFT.value,
        )
        .order_by(Sale.invoice_date, Sale.invoice_no)
        .all()
    )
    sale_ids = [sale.id for sale in sales]

    lines = dict(
        db.query(SaleItem.sale_id, func.count(SaleItem.id))
        .filter(SaleItem.sale_id.in_(sale_ids))
        .group_by(SaleItem.sale_id)
        .all()
    ) if sale_ids else {}

    modes = _payment_modes_by_sale(db, sale_ids)

    rows = []
    for sale in sales:
        voided = sale.status == SaleStatus.VOIDED.value
        tax = sale.cgst_amount + sale.sgst_amount + sale.igst_amount
        rows.append(
            {
                "invoice_date": sale.invoice_date.isoformat(),
                "invoice_no": sale.invoice_no,
                "invoice_time": utc_iso(sale.invoice_datetime),
                "customer": sale.customer_name_snapshot
                or sale.customer_mobile_snapshot
                or "Walk-in",
                "sales_user": sale.sales_user_name or "",
                "line_count": int(lines.get(sale.id, 0)),
                # A voided invoice is shown, but it did not take any money.
                "gross_amount": money(ZERO if voided else sale.gross_amount),
                "discount_amount": money(ZERO if voided else sale.discount_amount),
                "taxable_amount": money(ZERO if voided else sale.taxable_amount),
                "tax_amount": money(ZERO if voided else tax),
                "round_off": money(ZERO if voided else sale.round_off),
                "net_amount": money(ZERO if voided else sale.net_amount),
                "payment_modes": "" if voided else modes.get(sale.id, ""),
                "status": sale.status,
            }
        )

    return ReportTable(
        key="sales_daybook",
        title="Sales day-book",
        columns=DAYBOOK_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows,
            money_keys=[
                "gross_amount", "discount_amount", "taxable_amount",
                "tax_amount", "round_off", "net_amount",
            ],
            int_keys=["line_count"],
        ),
        meta={
            "payment_modes": _collection_by_mode(db, store_id, date_from, date_to),
            "cash_refunds": _cash_refunds(db, store_id, date_from, date_to),
            "invoice_count": sum(1 for s in sales if s.status != SaleStatus.VOIDED.value),
            "voided_count": sum(1 for s in sales if s.status == SaleStatus.VOIDED.value),
        },
    )


def _payment_modes_by_sale(db: Session, sale_ids: list[UUID]) -> dict[UUID, str]:
    """{sale_id: "CASH 100.00, UPI 50.00"} — how each invoice was settled."""
    if not sale_ids:
        return {}

    rows = (
        db.query(Payment.sale_id, Payment.mode, func.sum(Payment.amount).label("amount"))
        .filter(Payment.sale_id.in_(sale_ids))
        .group_by(Payment.sale_id, Payment.mode)
        .order_by(Payment.sale_id, Payment.mode)
        .all()
    )

    collected: dict[UUID, list[str]] = {}
    for row in rows:
        collected.setdefault(row.sale_id, []).append(f"{row.mode} {money(row.amount)}")
    return {sale_id: ", ".join(parts) for sale_id, parts in collected.items()}


def _collection_by_mode(db: Session, store_id: UUID, date_from: date, date_to: date) -> list[dict]:
    """The drawer reconciliation: what came in, by tender.

    A credit-note redemption is listed with the rest because it settles an
    invoice, but it is no money in the till — the UI shows it separately.
    """
    rows = (
        db.query(
            Payment.mode,
            func.count(Payment.id).label("payment_count"),
            func.sum(Payment.amount).label("amount"),
        )
        .join(Sale, Payment.sale_id == Sale.id)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status.in_(LIVE_SALE_STATUSES),
        )
        .group_by(Payment.mode)
        .order_by(Payment.mode)
        .all()
    )
    return [
        {
            "mode": row.mode,
            "payment_count": int(row.payment_count or 0),
            "amount": money(row.amount),
        }
        for row in rows
    ]


def _cash_refunds(db: Session, store_id: UUID, date_from: date, date_to: date) -> str:
    """Money handed back over the counter, which the drawer has to account for."""
    total = (
        db.query(func.sum(SaleReturn.total_refund_amount))
        .filter(
            SaleReturn.store_id == store_id,
            SaleReturn.return_date >= date_from,
            SaleReturn.return_date <= date_to,
            SaleReturn.status == ReturnStatus.COMPLETED.value,
            SaleReturn.refund_mode == RefundMode.CASH.value,
        )
        .scalar()
    )
    return money(total)


# --- by executive --------------------------------------------------------


BY_EXECUTIVE_COLUMNS = [
    Column("sales_user", "Executive", ColumnType.TEXT),
    Column("invoice_count", "Invoices", ColumnType.INT),
    Column("units_sold", "Units sold", ColumnType.QTY),
    Column("gross_amount", "Gross", ColumnType.MONEY),
    Column("discount_amount", "Discount", ColumnType.MONEY),
    Column("discount_pct", "Discount %", ColumnType.RATE),
    Column("net_amount", "Net billed", ColumnType.MONEY),
    Column("average_invoice", "Average invoice", ColumnType.MONEY),
    Column("voided_count", "Voided", ColumnType.INT),
    Column("void_pct", "Void %", ColumnType.RATE),
    Column("returned_amount", "Returned value", ColumnType.MONEY),
    Column("return_pct", "Return %", ColumnType.RATE),
]


def by_executive(db: Session, store_id: UUID, date_from: date, date_to: date) -> ReportTable:
    """Per-executive volume, discounting, and how often it comes undone.

    The void and return percentages are the point of the report. Neither is
    an accusation on its own — a busy counter has both — but an executive
    well clear of the others on either is worth a conversation.
    """
    billed = (
        db.query(
            Sale.sales_user_id.label("user_id"),
            func.count(Sale.id).label("invoice_count"),
            func.sum(Sale.gross_amount).label("gross_amount"),
            func.sum(Sale.discount_amount).label("discount_amount"),
            func.sum(Sale.net_amount).label("net_amount"),
        )
        .filter(*_period(store_id, date_from, date_to), Sale.status.in_(LIVE_SALE_STATUSES))
        .group_by(Sale.sales_user_id)
        .all()
    )

    voided = dict(
        db.query(Sale.sales_user_id, func.count(Sale.id))
        .filter(*_period(store_id, date_from, date_to), Sale.status == SaleStatus.VOIDED.value)
        .group_by(Sale.sales_user_id)
        .all()
    )

    units = dict(
        db.query(Sale.sales_user_id, func.sum(SaleItem.qty_units))
        .join(SaleItem, SaleItem.sale_id == Sale.id)
        .filter(*_period(store_id, date_from, date_to), Sale.status.in_(LIVE_SALE_STATUSES))
        .group_by(Sale.sales_user_id)
        .all()
    )

    # Returns are attributed to whoever billed the original invoice, not to
    # whoever accepted the goods back.
    returned = dict(
        db.query(Sale.sales_user_id, func.sum(SaleReturn.total_refund_amount))
        .join(SaleReturn, SaleReturn.sale_id == Sale.id)
        .filter(
            Sale.store_id == store_id,
            SaleReturn.return_date >= date_from,
            SaleReturn.return_date <= date_to,
            SaleReturn.status == ReturnStatus.COMPLETED.value,
        )
        .group_by(Sale.sales_user_id)
        .all()
    )

    user_ids = set(voided) | set(returned) | {row.user_id for row in billed}
    names = dict(
        db.query(User.id, User.full_name).filter(User.id.in_(user_ids)).all()
    ) if user_ids else {}
    billed_by_user = {row.user_id: row for row in billed}

    rows = []
    for user_id in user_ids:
        row = billed_by_user.get(user_id)
        invoice_count = int(row.invoice_count) if row else 0
        gross = (row.gross_amount if row else None) or ZERO
        net = (row.net_amount if row else None) or ZERO
        void_count = int(voided.get(user_id, 0))
        rows.append(
            {
                "sales_user": names.get(user_id) or f"User #{user_id}",
                "invoice_count": invoice_count,
                "units_sold": qty(units.get(user_id)),
                "gross_amount": money(gross),
                "discount_amount": money((row.discount_amount if row else None) or ZERO),
                "discount_pct": pct((row.discount_amount if row else None) or ZERO, gross),
                "net_amount": money(net),
                "average_invoice": money(net / invoice_count) if invoice_count else money(0),
                "voided_count": void_count,
                "void_pct": pct(void_count, invoice_count + void_count),
                "returned_amount": money(returned.get(user_id)),
                "return_pct": pct(returned.get(user_id) or ZERO, net),
            }
        )

    rows.sort(key=lambda r: Decimal(r["net_amount"]), reverse=True)

    totals = sum_columns(
        rows,
        money_keys=["gross_amount", "discount_amount", "net_amount", "returned_amount"],
        int_keys=["invoice_count", "voided_count"],
        qty_keys=["units_sold"],
    )
    # Percentages are recomputed from the totals, never summed down a column.
    totals["discount_pct"] = pct(totals["discount_amount"], totals["gross_amount"])
    totals["void_pct"] = pct(
        totals["voided_count"], totals["invoice_count"] + totals["voided_count"]
    )
    totals["return_pct"] = pct(totals["returned_amount"], totals["net_amount"])
    totals["average_invoice"] = (
        money(Decimal(totals["net_amount"]) / totals["invoice_count"])
        if totals["invoice_count"]
        else money(0)
    )

    return ReportTable(
        key="sales_by_executive",
        title="Sales by executive",
        columns=BY_EXECUTIVE_COLUMNS,
        rows=rows,
        totals=totals,
    )


def _period(store_id: UUID, date_from: date, date_to: date):
    return (
        Sale.store_id == store_id,
        Sale.invoice_date >= date_from,
        Sale.invoice_date <= date_to,
    )
