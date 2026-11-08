"""Invoices: confirming a sale, the register, and the printable document.

There is deliberately **no** `DELETE` here and never will be. A printed
invoice is a financial document; corrections happen through the void
workflow or a return (Phase 5), both of which leave the original standing
(CLAUDE.md rule 5).

Cost and margin exist on every line but are served only to an admin, by
returning a different response model rather than blanking fields.
"""

from uuid import UUID

from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func
from sqlalchemy.orm import object_session, selectinload

from app.core.deps import CurrentSettings, CurrentUser, DbSession
from app.core.exceptions import NotFoundError
from app.db.types import quantize_money, quantize_qty
from app.models.enums import NO_EXPIRY, AuditAction, PaymentMode, SaleStatus
from app.models.returns import CreditNote
from app.models.sale import Payment, Sale, SaleItem
from app.models.store import Store
from app.api.v1 import returns as returns_api
from app.schemas.common import Page, PageParams
from app.schemas.returns import VoidRequestIn, VoidRequestOut
from app.schemas.sale import (
    InvoicePrint,
    PaymentOut,
    PrintRecord,
    SaleCreate,
    SaleItemAdmin,
    SaleItemOut,
    SaleOut,
    SaleRow,
    StoreHeader,
    TaxSummaryRow,
)
from app.services import audit, order_book, sales
from app.utils.money_words import amount_in_words
from app.utils.clock import utc_iso

router = APIRouter()

AUDIT_FIELDS = [
    "id", "invoice_no", "invoice_date", "customer_id", "customer_mobile_snapshot",
    "doctor_id", "sales_user_id", "gross_amount", "discount_amount", "taxable_amount",
    "cgst_amount", "sgst_amount", "igst_amount", "round_off", "net_amount", "status",
]


def _item_out(item: SaleItem, *, as_admin: bool):
    common = {
        "id": item.id,
        "line_no": item.line_no,
        "medicine_id": item.medicine_id,
        "medicine_name": item.medicine_name_snapshot,
        "item_type": item.item_type_snapshot,
        "pack_label": item.pack_label_snapshot,
        "hsn_code": item.hsn_code_snapshot,
        "batch_id": item.batch_id,
        "batch_no": item.batch_no_snapshot,
        "expiry_date": item.expiry_date_snapshot,
        # Goods with no shelf life carry the sentinel date. Say so, rather
        # than leaving the bill to print "9999-12".
        "has_expiry": item.expiry_date_snapshot != NO_EXPIRY,
        "units_per_pack": item.units_per_pack_snapshot,
        "qty_units": item.qty_units,
        "qty_packs": quantize_qty(
            item.qty_units / Decimal(item.units_per_pack_snapshot or 1)
        ),
        "unit_mrp": item.unit_mrp,
        "mrp_per_pack": item.mrp_per_pack,
        "gross_amount": item.gross_amount,
        "discount_pct": item.discount_pct,
        "discount_amount": item.discount_amount,
        "net_amount": item.net_amount,
        "gst_rate": item.gst_rate,
        "taxable_value": item.taxable_value,
        "cgst_amount": item.cgst_amount,
        "sgst_amount": item.sgst_amount,
        "igst_amount": item.igst_amount,
        "returned_qty_units": item.returned_qty_units,
    }
    if not as_admin:
        return SaleItemOut(**common)

    line_cost = margin = margin_pct = None
    if item.unit_cost_snapshot is not None:
        line_cost = quantize_money(item.unit_cost_snapshot * item.qty_units)
        margin = quantize_money(item.net_amount - line_cost)
        if line_cost > 0:
            margin_pct = quantize_money(margin / line_cost * Decimal(100))
    return SaleItemAdmin(
        **common,
        unit_cost=item.unit_cost_snapshot,
        line_cost=line_cost,
        margin_amount=margin,
        margin_pct=margin_pct,
    )


def _row(sale: Sale, item_count: int) -> SaleRow:
    return SaleRow(
        id=sale.id,
        invoice_no=sale.invoice_no,
        invoice_date=sale.invoice_date,
        invoice_datetime=utc_iso(sale.invoice_datetime),
        customer_mobile=sale.customer_mobile_snapshot,
        customer_name=sale.customer_name_snapshot,
        doctor_name=sale.doctor_name_snapshot,
        sales_user_name=sale.sales_user_name,
        item_count=item_count,
        gross_amount=sale.gross_amount,
        discount_amount=sale.discount_amount,
        net_amount=sale.net_amount,
        status=sale.status,
        reprint_count=sale.reprint_count,
    )


def _detail(sale: Sale, *, as_admin: bool, warnings: list[str] | None = None) -> SaleOut:
    base = _row(sale, len(sale.items))
    return SaleOut(
        **base.model_dump(),
        customer_id=sale.customer_id,
        customer_gstin=sale.customer_gstin_snapshot,
        doctor_id=sale.doctor_id,
        prescription_ref=sale.prescription_ref,
        is_intra_state=sale.is_intra_state,
        taxable_amount=sale.taxable_amount,
        cgst_amount=sale.cgst_amount,
        sgst_amount=sale.sgst_amount,
        igst_amount=sale.igst_amount,
        round_off=sale.round_off,
        paid_amount=sale.paid_amount,
        discount_override_reason=sale.discount_override_reason,
        notes=sale.notes,
        amount_in_words=amount_in_words(sale.net_amount),
        items=[_item_out(i, as_admin=as_admin) for i in sale.items],
        payments=[_payment_out(p) for p in sale.payments],
        warnings=warnings or [],
    )


def _payment_out(payment: Payment) -> PaymentOut:
    """One payment line, with the credit note named where there was one."""
    out = PaymentOut.model_validate(payment)
    if payment.credit_note_id:
        note = object_session(payment).get(CreditNote, payment.credit_note_id)
        if note is not None:
            out.credit_note_no = note.credit_note_no
            out.credit_note_balance = note.balance_amount
    return out


# --- confirming a sale ---------------------------------------------------


@router.post("", response_model=SaleOut, status_code=status.HTTP_201_CREATED)
def create_sale(
    payload: SaleCreate,
    request: Request,
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
) -> SaleOut:
    """Issue an invoice: allocate batches, decrement stock, take payment.

    Everything happens in one transaction. A repeated submit carrying the
    same `idempotency_key` returns the invoice already issued rather than
    billing the customer a second time.
    """
    existing = sales.find_by_idempotency_key(db, user.store_id, payload.idempotency_key)
    if existing is not None:
        return _detail(
            existing,
            as_admin=user.is_admin,
            warnings=[
                f"This sale was already billed as invoice {existing.invoice_no}; "
                f"the same invoice is shown again rather than a second one."
            ],
        )

    sale = sales.confirm_sale(
        db, store_id=user.store_id, payload=payload, settings=settings, user=user
    )

    # Selling is what moves stock past the reorder threshold, so this is the
    # moment to notice (SPEC §3.9). Only the medicines just sold are
    # re-checked — a full sweep on every sale would be wasteful.
    order_book.evaluate_medicines(
        db,
        store_id=user.store_id,
        medicine_ids=[item.medicine_id for item in sale.items],
        settings=settings,
        actor_id=user.id,
    )

    audit.record(
        db, action=AuditAction.CREATE, entity_type="Sale", entity_id=sale.id, user=user,
        after=audit.snapshot(sale, AUDIT_FIELDS),
        reason=sale.discount_override_reason, request=request,
    )
    db.commit()
    db.refresh(sale)
    return _detail(sale, as_admin=user.is_admin)


# --- the register --------------------------------------------------------


@router.get("", response_model=Page[SaleRow])
def sales_register(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = Query(None, description="Invoice number or mobile number"),
    date_from: date | None = None,
    date_to: date | None = None,
    status_filter: SaleStatus | None = Query(None, alias="status"),
    sales_user_id: UUID | None = None,
) -> Page[SaleRow]:
    """Every sale, newest first. `q` matches an invoice number **or** a mobile.

    Both users see the whole register: the shop needs any staff member to be
    able to find a customer's bill. What differs is that cost and margin
    never appear on the detail for a sales executive.
    """
    query = db.query(Sale).filter(Sale.store_id == user.store_id)
    if q:
        needle = f"%{q.strip()}%"
        query = query.filter(
            Sale.invoice_no.like(needle) | Sale.customer_mobile_snapshot.like(needle)
        )
    if date_from is not None:
        query = query.filter(Sale.invoice_date >= date_from)
    if date_to is not None:
        query = query.filter(Sale.invoice_date <= date_to)
    if status_filter is not None:
        query = query.filter(Sale.status == status_filter.value)
    if sales_user_id is not None:
        query = query.filter(Sale.sales_user_id == sales_user_id)

    total = query.count()
    rows = (
        query.order_by(Sale.invoice_datetime.desc(), Sale.id.desc())
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    counts = dict(
        db.query(SaleItem.sale_id, func.count(SaleItem.id))
        .filter(SaleItem.sale_id.in_([r.id for r in rows]))
        .group_by(SaleItem.sale_id)
        .all()
    )
    return Page.build([_row(r, counts.get(r.id, 0)) for r in rows], total, params)


@router.get("/by-invoice/{invoice_no}", response_model=SaleOut)
def sale_by_invoice(invoice_no: str, user: CurrentUser, db: DbSession) -> SaleOut:
    sale = (
        db.query(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .filter(Sale.store_id == user.store_id, Sale.invoice_no == invoice_no.strip())
        .one_or_none()
    )
    if sale is None:
        raise NotFoundError("No invoice with that number.", field="invoice_no")
    return _detail(sale, as_admin=user.is_admin)


@router.get("/{sale_id}", response_model=SaleOut)
def get_sale(sale_id: UUID, user: CurrentUser, db: DbSession) -> SaleOut:
    return _detail(_load(db, sale_id, user.store_id), as_admin=user.is_admin)


# --- printing ------------------------------------------------------------


@router.get("/{sale_id}/print", response_model=InvoicePrint)
def print_sale(
    sale_id: UUID,
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
) -> InvoicePrint:
    """The printable payload. The SPA renders it — the server sends no HTML.

    This is a **read**. Looking at a bill on screen is not printing it, and it
    used to be counted as one: opening the invoice screen bumped the counter
    whether or not any paper moved, so the register's figure measured glances
    rather than copies. The client now posts to `/printed` at the moment the
    operator commits to printing.
    """
    sale = _load(db, sale_id, user.store_id)
    store = db.get(Store, user.store_id)

    # A credit note is a payment, not a discount: the invoice stands for its
    # full value and the tax on it is unchanged. What differs is how much
    # the customer hands over, and that is the figure the bill has to make
    # obvious.
    credit_applied = quantize_money(
        sum(
            (p.amount for p in sale.payments if p.mode == PaymentMode.CREDIT_NOTE.value),
            Decimal("0"),
        )
    )
    payable = quantize_money(max(sale.net_amount - credit_applied, Decimal("0")))

    return InvoicePrint(
        store=StoreHeader(
            name=store.name,
            legal_name=store.legal_name,
            address=", ".join(
                p for p in [store.address_line1, store.address_line2] if p
            ) or None,
            city=store.city,
            state=store.state,
            pincode=store.pincode,
            gstin=store.gstin,
            drug_licence_no_20b=store.drug_licence_no_20b,
            drug_licence_no_21b=store.drug_licence_no_21b,
            phone=store.phone,
            email=store.email,
            logo_image=settings.logo_image,
            partner_logo_image=settings.partner_logo_image,
            order_qr_image=settings.order_qr_image,
            order_qr_caption=settings.order_qr_caption,
        ),
        sale=_detail(sale, as_admin=user.is_admin),
        tax_summary=_tax_summary(sale),
        amount_in_words=amount_in_words(sale.net_amount),
        credit_note_applied=credit_applied,
        amount_payable=payable,
        amount_payable_in_words=amount_in_words(payable),
        invoice_prefix=settings.invoice_prefix,
        footer_text=settings.invoice_footer_text,
        print_format=settings.print_format,
        gst_enabled=settings.gst_enabled,
        # Has it been printed before? Rendering this payload does not make it
        # so — only a recorded print does.
        is_reprint=sale.reprint_count > 0,
        reprint_count=sale.reprint_count,
        printed_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/{sale_id}/printed", response_model=PrintRecord)
def record_print(
    sale_id: UUID, request: Request, user: CurrentUser, db: DbSession
) -> PrintRecord:
    """Record that a bill has actually been sent to the printer.

    The first print is the original; every one after it is a duplicate, and
    the count is what an auditor reads when a customer turns up with two
    copies of the same invoice number. A jammed printer genuinely producing a
    second sheet is a second sheet, so it counts too.
    """
    sale = _load(db, sale_id, user.store_id)
    is_reprint = sale.reprint_count > 0

    sale.reprint_count += 1
    audit.record(
        db, action=AuditAction.PRINT, entity_type="Sale", entity_id=sale.id, user=user,
        after={"invoice_no": sale.invoice_no, "reprint_count": sale.reprint_count},
        request=request,
    )
    db.commit()
    db.refresh(sale)

    return PrintRecord(
        invoice_no=sale.invoice_no,
        reprint_count=sale.reprint_count,
        is_reprint=is_reprint,
        printed_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/{sale_id}/void-request", response_model=VoidRequestOut, status_code=201)
def request_void(
    sale_id: UUID, payload: VoidRequestIn, request: Request, user: CurrentUser, db: DbSession
) -> VoidRequestOut:
    """Ask an admin to cancel this invoice.

    Requesting is all a sales executive can do. The approval sits with an
    admin so that whoever took the money cannot also erase the record of it
    — see app/services/voids.py.
    """
    return returns_api.request_void_for_sale(
        db, sale=_load(db, sale_id, user.store_id), payload=payload, user=user, request=request
    )


def _tax_summary(sale: Sale) -> list[TaxSummaryRow]:
    """Tax grouped by rate — what a GST invoice has to show."""
    buckets: dict[Decimal, dict] = {}
    for item in sale.items:
        bucket = buckets.setdefault(
            item.gst_rate,
            {"taxable": Decimal("0"), "cgst": Decimal("0"),
             "sgst": Decimal("0"), "igst": Decimal("0")},
        )
        bucket["taxable"] += item.taxable_value
        bucket["cgst"] += item.cgst_amount
        bucket["sgst"] += item.sgst_amount
        bucket["igst"] += item.igst_amount

    return [
        TaxSummaryRow(
            gst_rate=rate,
            taxable_value=quantize_money(v["taxable"]),
            cgst_amount=quantize_money(v["cgst"]),
            sgst_amount=quantize_money(v["sgst"]),
            igst_amount=quantize_money(v["igst"]),
            total_tax=quantize_money(v["cgst"] + v["sgst"] + v["igst"]),
        )
        for rate, v in sorted(buckets.items())
    ]


def _load(db, sale_id: UUID, store_id: UUID) -> Sale:
    sale = (
        db.query(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .filter(Sale.id == sale_id)
        .one_or_none()
    )
    if sale is None or sale.store_id != store_id:
        raise NotFoundError("Sale not found.")
    return sale
