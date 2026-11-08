"""The three GST reports: outward, inward, and what is owed on the two.

These are the reports the shop's accountant asks for, so the figures here
have to agree with the documents already issued — not be recomputed from
today's rates. Every amount is summed from the **snapshots** taken when the
document was written: the sale line's `gst_rate` and tax split, the purchase
line's as billed by the supplier. Change a medicine's GST rate tomorrow and
last quarter's return is unaffected, which is the whole reason those columns
are snapshotted.

Three decisions worth stating, because they are what make the numbers right:

* **A voided invoice never happened.** It is excluded outright rather than
  netted off, because a void reverses the whole document including its
  number. A *return* is different: the goods were sold and then came back,
  so it becomes a credit note that reduces the period's liability — netted,
  not erased.
* **A credit note falls in the period it was raised**, by `return_date`, even
  when the invoice it relates to was in an earlier one. That is how the
  outward tax on a return is actually adjusted.
* **B2B is decided by the GSTIN on the invoice**, snapshotted at the time of
  sale. A customer who registers later does not retrospectively turn last
  month's retail bills into B2B supplies.
"""

from __future__ import annotations

from uuid import UUID

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, case, distinct, extract, func
from sqlalchemy.orm import Session

from app.models.enums import PurchaseReturnStatus, ReturnStatus, SaleStatus
from app.models.party import Supplier
from app.models.purchase import PurchaseInvoice, PurchaseItem
from app.models.purchase_return import PurchaseReturn, PurchaseReturnItem
from app.models.returns import SaleReturn, SaleReturnItem
from app.models.sale import Sale, SaleItem
from app.models.store import Store
from app.services.reports import Column, ColumnType, ReportTable, money, rate, sum_columns

#: Statuses that are still a live tax invoice. A draft was never issued and a
#: void has been withdrawn; everything else — including a sale awaiting void
#: approval, and one that has been fully returned — is a document the shop
#: has handed over and must account for.
LIVE_SALE_STATUSES = (
    SaleStatus.COMPLETED.value,
    SaleStatus.PARTIALLY_RETURNED.value,
    SaleStatus.RETURNED.value,
    SaleStatus.VOID_REQUESTED.value,
)

B2B = "B2B"
B2C = "B2C"

ZERO = Decimal("0")


def _b2b_flag(column) -> case:
    """1 when the invoice carries a customer GSTIN, else 0.

    Compared against the empty string as well as NULL: a blank GSTIN field
    saved from a form is not a B2B supply.
    """
    return case((and_(column.isnot(None), column != ""), 1), else_=0)


def _sales_in_period(db: Session, store_id: UUID, date_from: date, date_to: date):
    return (
        db.query(SaleItem)
        .join(Sale, SaleItem.sale_id == Sale.id)
        .filter(
            Sale.store_id == store_id,
            Sale.invoice_date >= date_from,
            Sale.invoice_date <= date_to,
            Sale.status.in_(LIVE_SALE_STATUSES),
        )
    )


# --- outward (sales) -----------------------------------------------------


OUTWARD_COLUMNS = [
    Column("customer_type", "Supply type", ColumnType.TEXT),
    Column("gst_rate", "GST %", ColumnType.RATE),
    Column("invoice_count", "Invoices", ColumnType.INT),
    Column("taxable_value", "Taxable value", ColumnType.MONEY),
    Column("cgst_amount", "CGST", ColumnType.MONEY),
    Column("sgst_amount", "SGST", ColumnType.MONEY),
    Column("igst_amount", "IGST", ColumnType.MONEY),
    Column("total_tax", "Total tax", ColumnType.MONEY),
    Column("invoice_value", "Invoice value", ColumnType.MONEY),
    Column("credit_taxable", "Credit notes — taxable", ColumnType.MONEY),
    Column("credit_tax", "Credit notes — tax", ColumnType.MONEY),
    Column("net_taxable", "Net taxable", ColumnType.MONEY),
    Column("net_tax", "Net tax", ColumnType.MONEY),
]


def outward_summary(db: Session, store_id: UUID, date_from: date, date_to: date) -> ReportTable:
    """Rate-wise outward supply, split B2C/B2B, net of credit notes."""
    flag = _b2b_flag(Sale.customer_gstin_snapshot)

    sales = (
        _sales_in_period(db, store_id, date_from, date_to)
        .with_entities(
            flag.label("is_b2b"),
            SaleItem.gst_rate.label("gst_rate"),
            func.count(distinct(Sale.id)).label("invoice_count"),
            func.sum(SaleItem.taxable_value).label("taxable_value"),
            func.sum(SaleItem.cgst_amount).label("cgst_amount"),
            func.sum(SaleItem.sgst_amount).label("sgst_amount"),
            func.sum(SaleItem.igst_amount).label("igst_amount"),
            func.sum(SaleItem.net_amount).label("invoice_value"),
        )
        .group_by(flag, SaleItem.gst_rate)
        .all()
    )

    credits = _credit_note_totals(db, store_id, date_from, date_to)

    rows: list[dict] = []
    seen: set[tuple[int, Decimal]] = set()

    for row in sales:
        key = (int(row.is_b2b), row.gst_rate)
        seen.add(key)
        credit_taxable, credit_tax = credits.get(key, (ZERO, ZERO))
        tax = (row.cgst_amount or ZERO) + (row.sgst_amount or ZERO) + (row.igst_amount or ZERO)
        rows.append(
            {
                "customer_type": B2B if row.is_b2b else B2C,
                "gst_rate": rate(row.gst_rate),
                "invoice_count": int(row.invoice_count or 0),
                "taxable_value": money(row.taxable_value),
                "cgst_amount": money(row.cgst_amount),
                "sgst_amount": money(row.sgst_amount),
                "igst_amount": money(row.igst_amount),
                "total_tax": money(tax),
                "invoice_value": money(row.invoice_value),
                "credit_taxable": money(credit_taxable),
                "credit_tax": money(credit_tax),
                "net_taxable": money((row.taxable_value or ZERO) - credit_taxable),
                "net_tax": money(tax - credit_tax),
            }
        )

    # A return raised this period against an invoice from an earlier one has
    # no sales row to sit on, and must still reduce the liability.
    for (is_b2b, gst_rate), (credit_taxable, credit_tax) in sorted(credits.items()):
        if (is_b2b, gst_rate) in seen:
            continue
        rows.append(
            {
                "customer_type": B2B if is_b2b else B2C,
                "gst_rate": rate(gst_rate),
                "invoice_count": 0,
                "taxable_value": money(0),
                "cgst_amount": money(0),
                "sgst_amount": money(0),
                "igst_amount": money(0),
                "total_tax": money(0),
                "invoice_value": money(0),
                "credit_taxable": money(credit_taxable),
                "credit_tax": money(credit_tax),
                "net_taxable": money(-credit_taxable),
                "net_tax": money(-credit_tax),
            }
        )

    rows.sort(key=lambda r: (r["customer_type"], Decimal(r["gst_rate"])))

    return ReportTable(
        key="gst_outward_summary",
        title="GST outward (sales) summary",
        columns=OUTWARD_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows,
            money_keys=[
                "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "total_tax",
                "invoice_value", "credit_taxable", "credit_tax", "net_taxable", "net_tax",
            ],
            int_keys=["invoice_count"],
        ),
        meta=_gst_meta(db, store_id)
        | {
            # Round-off is per invoice, not per rate, so it cannot be shown in
            # a rate-wise row without inventing a split. It is reported here
            # so invoice value + round-off reconciles to what was collected.
            "round_off_total": money(
                db.query(func.sum(Sale.round_off))
                .filter(
                    Sale.store_id == store_id,
                    Sale.invoice_date >= date_from,
                    Sale.invoice_date <= date_to,
                    Sale.status.in_(LIVE_SALE_STATUSES),
                )
                .scalar()
            ),
        },
    )


def _credit_note_totals(
    db: Session, store_id: UUID, date_from: date, date_to: date
) -> dict[tuple[int, Decimal], tuple[Decimal, Decimal]]:
    """{(is_b2b, gst_rate): (taxable, tax)} for returns raised in the period."""
    flag = _b2b_flag(Sale.customer_gstin_snapshot)

    rows = (
        db.query(
            flag.label("is_b2b"),
            SaleReturnItem.gst_rate.label("gst_rate"),
            func.sum(SaleReturnItem.taxable_value).label("taxable_value"),
            func.sum(SaleReturnItem.cgst_amount).label("cgst_amount"),
            func.sum(SaleReturnItem.sgst_amount).label("sgst_amount"),
            func.sum(SaleReturnItem.igst_amount).label("igst_amount"),
        )
        .join(SaleReturn, SaleReturnItem.sale_return_id == SaleReturn.id)
        .join(Sale, SaleReturn.sale_id == Sale.id)
        .filter(
            SaleReturn.store_id == store_id,
            SaleReturn.return_date >= date_from,
            SaleReturn.return_date <= date_to,
            SaleReturn.status == ReturnStatus.COMPLETED.value,
        )
        .group_by(flag, SaleReturnItem.gst_rate)
        .all()
    )

    return {
        (int(row.is_b2b), row.gst_rate): (
            row.taxable_value or ZERO,
            (row.cgst_amount or ZERO) + (row.sgst_amount or ZERO) + (row.igst_amount or ZERO),
        )
        for row in rows
    }


# --- inward (purchases) --------------------------------------------------


INWARD_COLUMNS = [
    Column("supplier_name", "Supplier", ColumnType.TEXT),
    Column("supplier_gstin", "GSTIN", ColumnType.TEXT),
    # Purchases and returns are separate documents under GST — one claims
    # input tax, the other reverses it — so they get separate rows rather
    # than being netted into a figure neither side could reconcile. The
    # totals row nets them, which is the ITC actually available.
    Column("document", "Document", ColumnType.TEXT),
    Column("gst_rate", "GST %", ColumnType.RATE),
    Column("invoice_count", "Invoices", ColumnType.INT),
    Column("taxable_value", "Taxable value", ColumnType.MONEY),
    Column("cgst_amount", "CGST", ColumnType.MONEY),
    Column("sgst_amount", "SGST", ColumnType.MONEY),
    Column("igst_amount", "IGST", ColumnType.MONEY),
    Column("total_tax", "Total tax", ColumnType.MONEY),
    Column("invoice_value", "Invoice value", ColumnType.MONEY),
]


def inward_summary(db: Session, store_id: UUID, date_from: date, date_to: date) -> ReportTable:
    """The purchase register: supplier and rate-wise input tax."""
    rows_raw = (
        db.query(
            Supplier.name.label("supplier_name"),
            Supplier.gstin.label("supplier_gstin"),
            PurchaseItem.gst_rate.label("gst_rate"),
            func.count(distinct(PurchaseInvoice.id)).label("invoice_count"),
            func.sum(PurchaseItem.taxable_value).label("taxable_value"),
            func.sum(PurchaseItem.cgst_amount).label("cgst_amount"),
            func.sum(PurchaseItem.sgst_amount).label("sgst_amount"),
            func.sum(PurchaseItem.igst_amount).label("igst_amount"),
            func.sum(PurchaseItem.line_total).label("invoice_value"),
        )
        .join(PurchaseInvoice, PurchaseItem.purchase_invoice_id == PurchaseInvoice.id)
        .join(Supplier, PurchaseInvoice.supplier_id == Supplier.id)
        .filter(
            PurchaseInvoice.store_id == store_id,
            PurchaseInvoice.received_date >= date_from,
            PurchaseInvoice.received_date <= date_to,
            PurchaseInvoice.is_cancelled.is_(False),
        )
        .group_by(Supplier.id, Supplier.name, Supplier.gstin, PurchaseItem.gst_rate)
        .order_by(Supplier.name, PurchaseItem.gst_rate)
        .all()
    )

    rows = [
        {
            "supplier_name": row.supplier_name,
            "supplier_gstin": row.supplier_gstin or "",
            "document": "Purchase",
            "gst_rate": rate(row.gst_rate),
            "invoice_count": int(row.invoice_count or 0),
            "taxable_value": money(row.taxable_value),
            "cgst_amount": money(row.cgst_amount),
            "sgst_amount": money(row.sgst_amount),
            "igst_amount": money(row.igst_amount),
            "total_tax": money(
                (row.cgst_amount or ZERO) + (row.sgst_amount or ZERO) + (row.igst_amount or ZERO)
            ),
            "invoice_value": money(row.invoice_value),
        }
        for row in rows_raw
    ]

    rows.extend(_inward_return_rows(db, store_id, date_from, date_to))
    rows.sort(key=lambda r: (r["supplier_name"], r["gst_rate"], r["document"]))

    return ReportTable(
        key="gst_inward_summary",
        title="GST inward (purchase) summary",
        columns=INWARD_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows,
            money_keys=[
                "taxable_value", "cgst_amount", "sgst_amount", "igst_amount",
                "total_tax", "invoice_value",
            ],
            int_keys=["invoice_count"],
        ),
        meta=_gst_meta(db, store_id) | {"dated_by": "received_date"},
    )


def _inward_return_rows(
    db: Session, store_id: UUID, date_from: date, date_to: date
) -> list[dict]:
    """Goods sent back, as negative input tax.

    A debit note reverses credit already claimed, so every money figure is
    carried negative and the register's totals row becomes the input tax
    genuinely available for the period. Cancelled notes are excluded — the
    goods came back on the shelf and the credit was withdrawn.
    """
    raw = (
        db.query(
            Supplier.name.label("supplier_name"),
            Supplier.gstin.label("supplier_gstin"),
            PurchaseReturnItem.gst_rate.label("gst_rate"),
            func.count(distinct(PurchaseReturn.id)).label("note_count"),
            func.sum(PurchaseReturnItem.taxable_value).label("taxable_value"),
            func.sum(PurchaseReturnItem.cgst_amount).label("cgst_amount"),
            func.sum(PurchaseReturnItem.sgst_amount).label("sgst_amount"),
            func.sum(PurchaseReturnItem.igst_amount).label("igst_amount"),
            func.sum(PurchaseReturnItem.line_total).label("invoice_value"),
        )
        .join(PurchaseReturn, PurchaseReturnItem.purchase_return_id == PurchaseReturn.id)
        .join(Supplier, PurchaseReturn.supplier_id == Supplier.id)
        .filter(
            PurchaseReturn.store_id == store_id,
            PurchaseReturn.return_date >= date_from,
            PurchaseReturn.return_date <= date_to,
            PurchaseReturn.status == PurchaseReturnStatus.COMPLETED.value,
        )
        .group_by(Supplier.id, Supplier.name, Supplier.gstin, PurchaseReturnItem.gst_rate)
        .all()
    )

    def negative(value) -> str:
        return money(-(value or ZERO))

    return [
        {
            "supplier_name": row.supplier_name,
            "supplier_gstin": row.supplier_gstin or "",
            "document": "Return (debit note)",
            "gst_rate": rate(row.gst_rate),
            "invoice_count": int(row.note_count or 0),
            "taxable_value": negative(row.taxable_value),
            "cgst_amount": negative(row.cgst_amount),
            "sgst_amount": negative(row.sgst_amount),
            "igst_amount": negative(row.igst_amount),
            "total_tax": negative(
                (row.cgst_amount or ZERO) + (row.sgst_amount or ZERO) + (row.igst_amount or ZERO)
            ),
            "invoice_value": negative(row.invoice_value),
        }
        for row in raw
    ]


# --- net liability -------------------------------------------------------


LIABILITY_COLUMNS = [
    Column("month", "Month", ColumnType.TEXT),
    Column("gst_rate", "GST %", ColumnType.RATE),
    Column("outward_taxable", "Outward taxable", ColumnType.MONEY),
    Column("outward_tax", "Outward tax", ColumnType.MONEY),
    Column("inward_taxable", "Inward taxable", ColumnType.MONEY),
    Column("input_credit", "Input credit", ColumnType.MONEY),
    Column("net_liability", "Net payable", ColumnType.MONEY),
]


def net_liability(db: Session, store_id: UUID, date_from: date, date_to: date) -> ReportTable:
    """Outward tax less input credit, per rate, per month.

    Outward is already net of the period's credit notes; a month where more
    came back than went out shows a negative figure rather than being clamped
    at zero, because that credit is genuinely carried forward.
    """
    outward = _monthly(
        db,
        _sales_in_period(db, store_id, date_from, date_to).with_entities(
            *_month_parts(Sale.invoice_date),
            SaleItem.gst_rate.label("gst_rate"),
            func.sum(SaleItem.taxable_value).label("taxable_value"),
            func.sum(SaleItem.cgst_amount).label("cgst_amount"),
            func.sum(SaleItem.sgst_amount).label("sgst_amount"),
            func.sum(SaleItem.igst_amount).label("igst_amount"),
        ).group_by(*_month_parts(Sale.invoice_date), SaleItem.gst_rate),
    )

    returned = _monthly(
        db,
        db.query(
            *_month_parts(SaleReturn.return_date),
            SaleReturnItem.gst_rate.label("gst_rate"),
            func.sum(SaleReturnItem.taxable_value).label("taxable_value"),
            func.sum(SaleReturnItem.cgst_amount).label("cgst_amount"),
            func.sum(SaleReturnItem.sgst_amount).label("sgst_amount"),
            func.sum(SaleReturnItem.igst_amount).label("igst_amount"),
        )
        .join(SaleReturn, SaleReturnItem.sale_return_id == SaleReturn.id)
        .filter(
            SaleReturn.store_id == store_id,
            SaleReturn.return_date >= date_from,
            SaleReturn.return_date <= date_to,
            SaleReturn.status == ReturnStatus.COMPLETED.value,
        )
        .group_by(*_month_parts(SaleReturn.return_date), SaleReturnItem.gst_rate),
    )

    inward = _monthly(
        db,
        db.query(
            *_month_parts(PurchaseInvoice.received_date),
            PurchaseItem.gst_rate.label("gst_rate"),
            func.sum(PurchaseItem.taxable_value).label("taxable_value"),
            func.sum(PurchaseItem.cgst_amount).label("cgst_amount"),
            func.sum(PurchaseItem.sgst_amount).label("sgst_amount"),
            func.sum(PurchaseItem.igst_amount).label("igst_amount"),
        )
        .join(PurchaseInvoice, PurchaseItem.purchase_invoice_id == PurchaseInvoice.id)
        .filter(
            PurchaseInvoice.store_id == store_id,
            PurchaseInvoice.received_date >= date_from,
            PurchaseInvoice.received_date <= date_to,
            PurchaseInvoice.is_cancelled.is_(False),
        )
        .group_by(*_month_parts(PurchaseInvoice.received_date), PurchaseItem.gst_rate),
    )

    # Goods sent back to a distributor reverse credit already claimed, so they
    # reduce the input side exactly as a customer's return reduces the
    # outward side. Leaving them out would overstate the credit available and
    # understate what is payable.
    sent_back = _monthly(
        db,
        db.query(
            *_month_parts(PurchaseReturn.return_date),
            PurchaseReturnItem.gst_rate.label("gst_rate"),
            func.sum(PurchaseReturnItem.taxable_value).label("taxable_value"),
            func.sum(PurchaseReturnItem.cgst_amount).label("cgst_amount"),
            func.sum(PurchaseReturnItem.sgst_amount).label("sgst_amount"),
            func.sum(PurchaseReturnItem.igst_amount).label("igst_amount"),
        )
        .join(PurchaseReturn, PurchaseReturnItem.purchase_return_id == PurchaseReturn.id)
        .filter(
            PurchaseReturn.store_id == store_id,
            PurchaseReturn.return_date >= date_from,
            PurchaseReturn.return_date <= date_to,
            PurchaseReturn.status == PurchaseReturnStatus.COMPLETED.value,
        )
        .group_by(*_month_parts(PurchaseReturn.return_date), PurchaseReturnItem.gst_rate),
    )

    rows = []
    for key in sorted(set(outward) | set(returned) | set(inward) | set(sent_back)):
        month, gst_rate = key
        out_taxable, out_tax = outward.get(key, (ZERO, ZERO))
        ret_taxable, ret_tax = returned.get(key, (ZERO, ZERO))
        in_taxable, in_tax = inward.get(key, (ZERO, ZERO))
        back_taxable, back_tax = sent_back.get(key, (ZERO, ZERO))
        rows.append(
            {
                "month": month,
                "gst_rate": rate(gst_rate),
                "outward_taxable": money(out_taxable - ret_taxable),
                "outward_tax": money(out_tax - ret_tax),
                "inward_taxable": money(in_taxable - back_taxable),
                "input_credit": money(in_tax - back_tax),
                "net_liability": money((out_tax - ret_tax) - (in_tax - back_tax)),
            }
        )

    return ReportTable(
        key="gst_net_liability",
        title="GST net liability",
        columns=LIABILITY_COLUMNS,
        rows=rows,
        totals=sum_columns(
            rows,
            money_keys=[
                "outward_taxable", "outward_tax", "inward_taxable",
                "input_credit", "net_liability",
            ],
        ),
        meta=_gst_meta(db, store_id),
    )


def _month_parts(column):
    """Year and month as separate integers — portable, unlike `strftime`.

    SQLAlchemy compiles `extract` to the right thing on each dialect, which
    keeps this query working when the database becomes PostgreSQL.
    """
    return (
        extract("year", column).label("year"),
        extract("month", column).label("month"),
    )


def _monthly(db: Session, query) -> dict[tuple[str, Decimal], tuple[Decimal, Decimal]]:
    """{("2026-07", rate): (taxable, tax)} from a year/month-grouped query."""
    return {
        (f"{int(row.year):04d}-{int(row.month):02d}", row.gst_rate): (
            row.taxable_value or ZERO,
            (row.cgst_amount or ZERO) + (row.sgst_amount or ZERO) + (row.igst_amount or ZERO),
        )
        for row in query.all()
    }


def _gst_meta(db: Session, store_id: UUID) -> dict:
    """Who the return belongs to — a report printed without this is anonymous."""
    store = db.get(Store, store_id)
    return {
        "store_name": store.name if store else None,
        "gstin": (store.gstin if store else None) or "",
        "state_code": (store.state_code if store else None) or "",
    }
