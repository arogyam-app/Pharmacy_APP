"""Goods inward — receiving a supplier's invoice into stock.

Admin-only: this is where cost price enters the system.

A posted purchase is not editable. Its lines are a copy of someone else's
document and the stock they created is already on the shelf, so a mistake is
corrected by cancelling (which reverses the stock through the ledger and
stays on record) and re-entering (CLAUDE.md rule 5).
"""

from uuid import UUID

from datetime import date

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.core.deps import DbSession, get_store_settings, require_permission
from app.core.exceptions import NotFoundError
from app.models.catalog import Medicine
from app.models.enums import AuditAction, ItemType
from app.models.user import User
from app.models.party import Supplier
from app.models.purchase import PurchaseInvoice, PurchaseItem
from app.schemas.common import Page, PageParams
from app.api.v1.medicines import to_search_result
from app.schemas.catalog import FormHintOut, MedicineMatch, PackHintOut
from app.schemas.purchase import (
    ImportedLine,
    PurchaseImportPreview,
    PurchaseOutPublic,
    PurchaseRowPublic,
    redact,
    PurchaseCancel,
    PurchaseCreate,
    PurchaseItemOut,
    PurchaseItemsAdd,
    PurchaseOut,
    PurchaseRow,
    PurchaseUpdate,
)
from app.services import audit, goods_inward, inventory, order_book, purchases
from app.utils import spreadsheet
from app.utils.clock import utc_iso

#: Holding the Purchases module. An admin has it by default; an executive
#: only if one was granted it, and then the responses arrive cost-free.
#: Reading the register and booking a delivery in are separate: an
#: executive may be trusted with goods inward without being handed the
#: whole buying side. Cost is stripped from the response either way.
PurchaseReader = Annotated[
    User, Depends(require_permission("purchases:read", "purchases:receive"))
]
GoodsReceiver = Annotated[User, Depends(require_permission("purchases:receive"))]

router = APIRouter()

AUDIT_FIELDS = [
    "id", "doc_number", "supplier_id", "supplier_invoice_no", "supplier_invoice_date",
    "received_date", "subtotal_taxable", "cgst_amount", "sgst_amount", "igst_amount",
    "total_amount", "payment_status", "is_cancelled",
]


def _row(invoice: PurchaseInvoice, supplier_name: str | None, item_count: int) -> PurchaseRow:
    return PurchaseRow(
        id=invoice.id,
        doc_number=invoice.doc_number,
        supplier_id=invoice.supplier_id,
        supplier_name=supplier_name,
        supplier_invoice_no=invoice.supplier_invoice_no,
        supplier_invoice_date=invoice.supplier_invoice_date,
        received_date=invoice.received_date,
        item_count=item_count,
        is_intra_state=invoice.is_intra_state,
        subtotal_taxable=invoice.subtotal_taxable,
        cgst_amount=invoice.cgst_amount,
        sgst_amount=invoice.sgst_amount,
        igst_amount=invoice.igst_amount,
        total_amount=invoice.total_amount,
        payment_status=invoice.payment_status,
        is_cancelled=invoice.is_cancelled,
    )


def _register_row(invoice, supplier_name, item_count, as_admin: bool):
    row = _row(invoice, supplier_name, item_count)
    return row if as_admin else redact(PurchaseRowPublic, row)


def _detail(
    db,
    invoice: PurchaseInvoice,
    warnings: list[str] | None = None,
    *,
    as_admin: bool = True,
) -> PurchaseOut | PurchaseOutPublic:
    """The invoice as posted — cost-free unless the reader is an admin."""
    supplier = db.get(Supplier, invoice.supplier_id)
    names = dict(
        db.query(Medicine.id, Medicine.brand_name)
        .filter(Medicine.id.in_([i.medicine_id for i in invoice.items]))
        .all()
    )
    base = _row(invoice, supplier.name if supplier else None, len(invoice.items))
    full = PurchaseOut(
        **base.model_dump(),
        discount_amount=invoice.discount_amount,
        other_charges=invoice.other_charges,
        round_off=invoice.round_off,
        notes=invoice.notes,
        cancel_reason=invoice.cancel_reason,
        cancelled_at=utc_iso(invoice.cancelled_at),
        items=[
            PurchaseItemOut(
                **{
                    key: getattr(item, key)
                    for key in PurchaseItemOut.model_fields
                    if key != "medicine_name"
                },
                medicine_name=names.get(item.medicine_id),
            )
            for item in invoice.items
        ],
        warnings=warnings or [],
    )
    return full if as_admin else redact(PurchaseOutPublic, full)


@router.get("", response_model=Page[PurchaseRow | PurchaseRowPublic])
def list_purchases(
    user: PurchaseReader,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    supplier_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_cancelled: bool = True,
    item_type: ItemType | None = Query(
        None,
        description=(
            "Invoices carrying at least one line of this kind. An invoice may "
            "legitimately mix both, in which case it appears under either."
        ),
    ),
) -> Page[PurchaseRow]:
    """The purchase register — newest receipt first."""
    query = db.query(PurchaseInvoice).filter(PurchaseInvoice.store_id == user.store_id)
    if item_type is not None:
        query = query.filter(
            PurchaseInvoice.id.in_(
                db.query(PurchaseItem.purchase_invoice_id)
                .join(Medicine, PurchaseItem.medicine_id == Medicine.id)
                .filter(Medicine.item_type == item_type.value)
            )
        )
    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.filter(
            func.lower(PurchaseInvoice.supplier_invoice_no).like(needle)
            | func.lower(PurchaseInvoice.doc_number).like(needle)
        )
    if supplier_id is not None:
        query = query.filter(PurchaseInvoice.supplier_id == supplier_id)
    if date_from is not None:
        query = query.filter(PurchaseInvoice.received_date >= date_from)
    if date_to is not None:
        query = query.filter(PurchaseInvoice.received_date <= date_to)
    if not include_cancelled:
        query = query.filter(PurchaseInvoice.is_cancelled.is_(False))

    total = query.count()
    rows = (
        query.order_by(PurchaseInvoice.received_date.desc(), PurchaseInvoice.id.desc())
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )

    ids = [r.id for r in rows]
    names = dict(db.query(Supplier.id, Supplier.name).filter(Supplier.id.in_(
        [r.supplier_id for r in rows]
    )).all())
    counts = dict(
        db.query(PurchaseItem.purchase_invoice_id, func.count(PurchaseItem.id))
        .filter(PurchaseItem.purchase_invoice_id.in_(ids))
        .group_by(PurchaseItem.purchase_invoice_id)
        .all()
    )
    return Page.build(
        [
            _register_row(r, names.get(r.supplier_id), counts.get(r.id, 0), user.is_admin)
            for r in rows
        ],
        total,
        params,
    )


# --- receiving from a supplier's file ------------------------------------


@router.get("/import-template")
def purchase_import_template(
    user: PurchaseReader,
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
):
    """The blank item list a distributor's lines are pasted into.

    Items only. The supplier, the invoice number and its date are entered on
    the screen — they identify the document, and a file that carried them
    could name a supplier the operator never chose.
    """
    if format == "csv":
        return spreadsheet.csv_template(goods_inward.COLUMNS, filename="purchase-items.csv")
    return spreadsheet.xlsx_response(
        spreadsheet.xlsx_template(
            goods_inward.COLUMNS, title="Purchase items", guidance=goods_inward.GUIDANCE
        ),
        "purchase-items.xlsx",
    )


@router.post("/import-preview", response_model=PurchaseImportPreview)
async def preview_purchase_import(
    user: GoodsReceiver,
    db: DbSession,
    file: UploadFile = File(..., description="CSV or XLSX item list"),
) -> PurchaseImportPreview:
    """Read a supplier's item list and say what each line might mean.

    **Receives nothing.** A file is a claim about what arrived; the shop is
    the only party that can confirm it. So every row comes back parsed, with
    the products its text might mean and any problem with its figures, and
    the operator resolves them before the ordinary goods-inward call moves
    any stock.

    Matching is on tokens, so a trailing `TAB` or `SYP` and a different word
    order stop mattering. A pack size in the text is read out and checked
    separately: `Volini Gel 10GM` and `Volini Gel 30GM` are two products, so
    a row whose size is not on the master says so rather than settling for
    the nearest name.
    """
    raw = await file.read()
    spreadsheet.check_size(raw)
    headers, rows = spreadsheet.read_table(
        file.filename, raw, required=goods_inward.REQUIRED_HEADERS
    )
    missing = spreadsheet.missing_columns(headers, goods_inward.COLUMNS)

    settings = get_store_settings(db, user.store_id)
    lines = goods_inward.review(db, store_id=user.store_id, rows=rows)

    out: list[ImportedLine] = []
    ready = 0
    for line in lines:
        result = line.match
        candidates = result.candidates if result else []
        stock = inventory.stock_snapshot(
            db, user.store_id, [c.medicine.id for c in candidates], settings
        )
        matches = [
            MedicineMatch(
                medicine=to_search_result(c.medicine, settings, stock[c.medicine.id]),
                score=round(c.score, 4),
                pack_matches=c.pack_matches,
                form_matches=c.form_matches,
                note=c.note,
            )
            for c in candidates
        ]
        has_pack_match = result.has_pack_match if result else False
        # "Ready" only describes how much is left to do. It is not a licence
        # to skip the confirmation — every line is still shown and chosen.
        if not line.errors and has_pack_match and len(matches) == 1:
            ready += 1

        out.append(
            ImportedLine(
                row=line.row,
                product_text=line.product_text,
                batch_no=line.batch_no,
                expiry_basis=line.expiry_basis,
                expiry_date=line.expiry_date,
                mfg_date=line.mfg_date,
                best_before_months=line.best_before_months,
                qty_packs=line.qty_packs,
                free_qty_packs=line.free_qty_packs,
                mrp_per_pack=line.mrp_per_pack,
                cost_per_pack=line.cost_per_pack,
                discount_pct=line.discount_pct,
                gst_rate=line.gst_rate,
                default_discount_pct=line.default_discount_pct,
                max_discount_pct=line.max_discount_pct,
                matches=matches,
                pack_hint=(
                    PackHintOut(
                        text=result.pack_hint.text,
                        size=result.pack_hint.size,
                        unit=result.pack_hint.unit,
                    )
                    if result and result.pack_hint
                    else None
                ),
                has_pack_match=has_pack_match,
                form_hint=(
                    FormHintOut(text=result.form_hint.text, dosage_form=result.form_hint.dosage_form)
                    if result and result.form_hint
                    else None
                ),
                has_form_match=result.has_form_match if result else False,
                errors=line.errors,
            )
        )

    return PurchaseImportPreview(
        filename=file.filename,
        total_rows=len(out),
        ready_rows=ready,
        lines=out,
        missing_columns=missing,
    )


@router.post("/{purchase_id}/items", response_model=PurchaseOut | PurchaseOutPublic)
def add_purchase_items(
    purchase_id: UUID,
    payload: PurchaseItemsAdd,
    request: Request,
    user: GoodsReceiver,
    db: DbSession,
) -> PurchaseOut | PurchaseOutPublic:
    """Add lines to an invoice already received.

    Goods from one supplier invoice reach the shelf over more than one
    sitting, and a medicine and a general item on the same document are
    entered from two different screens. Lines can only be **added** — see
    `services.purchases.append_lines` for why that bound is where it is.
    """
    settings = get_store_settings(db, user.store_id)
    invoice = _load(db, purchase_id, user.store_id)
    before = audit.snapshot(invoice, AUDIT_FIELDS)

    warnings = purchases.append_lines(
        db, invoice=invoice, lines=payload.items, settings=settings, actor_id=user.id
    )

    closed = order_book.close_for_purchase(
        db,
        store_id=user.store_id,
        medicine_ids=[line.medicine_id for line in payload.items],
        purchase_invoice_id=invoice.id,
        actor_id=user.id,
    )
    if closed:
        warnings.append(f"{closed} order-book suggestion(s) closed by this delivery.")

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="PurchaseInvoice", entity_id=invoice.id,
        user=user, before=before, after=audit.snapshot(invoice, AUDIT_FIELDS),
        reason=f"{len(payload.items)} item(s) added to {invoice.doc_number}",
        request=request,
    )
    db.commit()
    db.refresh(invoice)
    return _detail(db, invoice, warnings, as_admin=user.is_admin)


@router.get("/{purchase_id}", response_model=PurchaseOut | PurchaseOutPublic)
def get_purchase(purchase_id: UUID, user: PurchaseReader, db: DbSession) -> PurchaseOut | PurchaseOutPublic:
    return _detail(db, _load(db, purchase_id, user.store_id), as_admin=user.is_admin)


@router.post("", response_model=PurchaseOut | PurchaseOutPublic, status_code=status.HTTP_201_CREATED)
def create_purchase(
    payload: PurchaseCreate, request: Request, user: GoodsReceiver, db: DbSession
) -> PurchaseOut | PurchaseOutPublic:
    """Receive goods: creates batches, ledger rows and the inward GST split."""
    settings = get_store_settings(db, user.store_id)
    supplier = db.get(Supplier, payload.supplier_id)
    if supplier is None or supplier.store_id != user.store_id:
        raise NotFoundError("Supplier not found.", field="supplier_id")

    invoice, warnings = purchases.post_purchase(
        db,
        store_id=user.store_id,
        supplier=supplier,
        payload=payload,
        settings=settings,
        actor_id=user.id,
    )

    # Goods arriving answer whatever the order book was asking for
    # (SPEC §3.9). The baseline was already reset by the posting service.
    closed = order_book.close_for_purchase(
        db,
        store_id=user.store_id,
        medicine_ids=[item.medicine_id for item in invoice.items],
        purchase_invoice_id=invoice.id,
        actor_id=user.id,
    )
    if closed:
        warnings.append(
            f"{closed} order-book suggestion(s) closed by this delivery."
        )

    audit.record(
        db, action=AuditAction.CREATE, entity_type="PurchaseInvoice", entity_id=invoice.id,
        user=user, after=audit.snapshot(invoice, AUDIT_FIELDS), request=request,
    )
    db.commit()
    db.refresh(invoice)
    return _detail(db, invoice, warnings, as_admin=user.is_admin)


@router.patch("/{purchase_id}", response_model=PurchaseOut | PurchaseOutPublic)
def update_purchase(
    purchase_id: UUID, payload: PurchaseUpdate, request: Request, user: GoodsReceiver, db: DbSession
) -> PurchaseOut | PurchaseOutPublic:
    """Payment status and notes only — the figures are the supplier's."""
    invoice = _load(db, purchase_id, user.store_id)
    before = audit.snapshot(invoice, AUDIT_FIELDS)

    changes = payload.model_dump(exclude_unset=True)
    if "payment_status" in changes and changes["payment_status"] is not None:
        changes["payment_status"] = str(changes["payment_status"])
    for field, value in changes.items():
        setattr(invoice, field, value)
    invoice.updated_by_id = user.id

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="PurchaseInvoice", entity_id=invoice.id,
        user=user, before=before, after=audit.snapshot(invoice, AUDIT_FIELDS), request=request,
    )
    db.commit()
    db.refresh(invoice)
    return _detail(db, invoice, as_admin=user.is_admin)


@router.post("/{purchase_id}/cancel", response_model=PurchaseOut | PurchaseOutPublic)
def cancel_purchase(
    purchase_id: UUID, payload: PurchaseCancel, request: Request, user: GoodsReceiver, db: DbSession
) -> PurchaseOut | PurchaseOutPublic:
    """Reverse a purchase entered in error, while nothing has been sold."""
    invoice = _load(db, purchase_id, user.store_id)
    before = audit.snapshot(invoice, AUDIT_FIELDS)

    purchases.cancel_purchase(db, invoice=invoice, reason=payload.reason, actor_id=user.id)

    audit.record(
        db, action=AuditAction.VOID, entity_type="PurchaseInvoice", entity_id=invoice.id,
        user=user, before=before, after=audit.snapshot(invoice, AUDIT_FIELDS),
        reason=payload.reason, request=request,
    )
    db.commit()
    db.refresh(invoice)
    return _detail(db, invoice, as_admin=user.is_admin)


def _load(db, purchase_id: UUID, store_id: UUID) -> PurchaseInvoice:
    invoice = (
        db.query(PurchaseInvoice)
        .options(selectinload(PurchaseInvoice.items))
        .filter(PurchaseInvoice.id == purchase_id)
        .one_or_none()
    )
    if invoice is None or invoice.store_id != store_id:
        raise NotFoundError("Purchase not found.")
    return invoice
