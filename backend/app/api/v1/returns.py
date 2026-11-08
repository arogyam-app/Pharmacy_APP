"""Returns, credit notes and the void-approval workflow.

Three routers live here because they are one story: a sale that has to be
undone, in whole (void) or in part (return), and the money that goes back
to the customer as a result (credit note).

Who may do what:
* a sales executive takes returns and *requests* a void;
* only an admin approves or rejects a void, approves an out-of-window
  return, or cancels a credit note.
"""

from uuid import UUID

from datetime import date

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.core.deps import AdminUser, CurrentSettings, CurrentUser, DbSession
from app.core.exceptions import ConflictError, NotFoundError
from app.models.enums import ApprovalStatus, AuditAction, CreditNoteStatus, SaleStatus
from app.models.party import Customer
from app.models.returns import (
    CreditNote,
    CreditNoteUsage,
    SaleReturn,
    SaleReturnItem,
    SaleVoidRequest,
)
from app.models.sale import Sale
from app.schemas.common import Page, PageParams
from app.schemas.returns import (
    CreditNoteCancelIn,
    CreditNoteOut,
    CreditNoteUsageOut,
    ReturnableItems,
    ReturnableLine,
    ReturnCreate,
    ReturnItemOut,
    ReturnOut,
    ReturnRow,
    VoidRejectIn,
    VoidRequestIn,
    VoidRequestOut,
    VoidReviewIn,
)
from app.services import audit, credit_notes, returns as returns_service, voids
from app.utils.clock import business_date, utc_iso

returns_router = APIRouter()
credit_notes_router = APIRouter()
void_router = APIRouter()


def _load_sale(db, sale_id: UUID, store_id: UUID) -> Sale:
    sale = (
        db.query(Sale)
        .options(selectinload(Sale.items), selectinload(Sale.payments))
        .filter(Sale.id == sale_id)
        .one_or_none()
    )
    if sale is None or sale.store_id != store_id:
        raise NotFoundError("Sale not found.", field="sale_id")
    return sale


# --- returns -------------------------------------------------------------


@returns_router.get("/returnable/{sale_id}", response_model=ReturnableItems)
def returnable(
    sale_id: UUID, user: CurrentUser, settings: CurrentSettings, db: DbSession
) -> ReturnableItems:
    """What is still returnable on an invoice, line by line."""
    sale = _load_sale(db, sale_id, user.store_id)
    rows = returns_service.returnable_items(db, sale)
    days = (business_date() - sale.invoice_date).days

    return ReturnableItems(
        sale_id=sale.id,
        invoice_no=sale.invoice_no,
        invoice_date=sale.invoice_date,
        customer_mobile=sale.customer_mobile_snapshot,
        customer_id=sale.customer_id,
        days_since_sale=days,
        return_window_days=settings.return_window_days,
        within_window=days <= settings.return_window_days,
        can_return=(
            sale.status != SaleStatus.VOIDED.value
            and any(r["qty_returnable_units"] > 0 for r in rows)
        ),
        items=[ReturnableLine(**row) for row in rows],
    )


@returns_router.post("", response_model=ReturnOut, status_code=status.HTTP_201_CREATED)
def create_return(
    payload: ReturnCreate,
    request: Request,
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
) -> ReturnOut:
    """Take goods back and settle the refund as cash or a credit note."""
    sale = _load_sale(db, payload.sale_id, user.store_id)

    sale_return = returns_service.post_return(
        db, store_id=user.store_id, sale=sale, payload=payload, settings=settings, user=user
    )
    note = returns_service.settle(
        db, sale_return=sale_return, sale=sale, settings=settings, user=user
    )

    audit.record(
        db, action=AuditAction.CREATE, entity_type="SaleReturn", entity_id=sale_return.id,
        user=user,
        after={
            "return_no": sale_return.return_no,
            "sale_id": sale.id,
            "invoice_no": sale.invoice_no,
            "total_refund_amount": sale_return.total_refund_amount,
            "refund_mode": sale_return.refund_mode,
            "credit_note_no": note.credit_note_no if note else None,
        },
        reason=sale_return.reason, request=request,
    )
    db.commit()
    db.refresh(sale_return)
    return _return_detail(db, sale_return)


@returns_router.get("", response_model=Page[ReturnRow])
def list_returns(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = Query(None, description="Return number or invoice number"),
    date_from: date | None = None,
    date_to: date | None = None,
) -> Page[ReturnRow]:
    query = db.query(SaleReturn).filter(SaleReturn.store_id == user.store_id)
    if q:
        needle = f"%{q.strip()}%"
        invoice_ids = [
            row[0]
            for row in db.query(Sale.id).filter(
                Sale.store_id == user.store_id, Sale.invoice_no.like(needle)
            )
        ]
        query = query.filter(
            SaleReturn.return_no.like(needle)
            | SaleReturn.sale_id.in_(invoice_ids)
        )
    if date_from is not None:
        query = query.filter(SaleReturn.return_date >= date_from)
    if date_to is not None:
        query = query.filter(SaleReturn.return_date <= date_to)

    total = query.count()
    rows = (
        query.order_by(SaleReturn.return_date.desc(), SaleReturn.id.desc())
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    return Page.build([_return_row(db, r) for r in rows], total, params)


@returns_router.get("/{return_id}", response_model=ReturnOut)
def get_return(return_id: UUID, user: CurrentUser, db: DbSession) -> ReturnOut:
    row = (
        db.query(SaleReturn)
        .options(selectinload(SaleReturn.items))
        .filter(SaleReturn.id == return_id)
        .one_or_none()
    )
    if row is None or row.store_id != user.store_id:
        raise NotFoundError("Return not found.")
    return _return_detail(db, row)


def _return_row(db, row: SaleReturn) -> ReturnRow:
    sale = db.get(Sale, row.sale_id)
    note = (
        db.query(CreditNote).filter(CreditNote.sale_return_id == row.id).one_or_none()
    )
    return ReturnRow(
        id=row.id,
        return_no=row.return_no,
        return_date=row.return_date,
        sale_id=row.sale_id,
        invoice_no=sale.invoice_no if sale else None,
        customer_mobile=sale.customer_mobile_snapshot if sale else None,
        refund_mode=row.refund_mode,
        total_refund_amount=row.total_refund_amount,
        status=row.status,
        item_count=len(row.items),
        credit_note_no=note.credit_note_no if note else None,
    )


def _return_detail(db, row: SaleReturn) -> ReturnOut:
    base = _return_row(db, row)
    note = db.query(CreditNote).filter(CreditNote.sale_return_id == row.id).one_or_none()

    warnings = []
    not_restocked = [i for i in row.items if not i.restocked]
    if not_restocked:
        warnings.append(
            f"{len(not_restocked)} line(s) came back damaged or expired and were "
            f"refunded without going back on sale."
        )
    if row.approved_by_name:
        warnings.append(
            f"Returned {row.days_since_sale} days after the sale — approved by "
            f"{row.approved_by_name}."
        )
    if note:
        warnings.append(
            f"Credit note {note.credit_note_no} issued for {note.amount_issued}, "
            f"valid to {note.expires_on}."
        )

    return ReturnOut(
        **base.model_dump(),
        reason=row.reason,
        gross_amount=row.gross_amount,
        discount_amount=row.discount_amount,
        taxable_amount=row.taxable_amount,
        cgst_amount=row.cgst_amount,
        sgst_amount=row.sgst_amount,
        igst_amount=row.igst_amount,
        processed_by_name=row.processed_by_name,
        approved_by_name=row.approved_by_name,
        days_since_sale=row.days_since_sale,
        restocked=row.restocked,
        credit_note_id=note.id if note else None,
        items=[
            ReturnItemOut(
                id=i.id,
                sale_item_id=i.sale_item_id,
                medicine_id=i.medicine_id,
                medicine_name=i.medicine_name_snapshot,
                batch_id=i.batch_id,
                batch_no=i.batch_no_snapshot,
                qty_units=i.qty_units,
                unit_mrp=i.unit_mrp,
                discount_pct=i.discount_pct,
                gst_rate=i.gst_rate,
                taxable_value=i.taxable_value,
                cgst_amount=i.cgst_amount,
                sgst_amount=i.sgst_amount,
                igst_amount=i.igst_amount,
                refund_amount=i.refund_amount,
                condition=i.condition,
                restocked=i.restocked,
            )
            for i in row.items
        ],
        warnings=warnings,
    )


# --- credit notes --------------------------------------------------------


@credit_notes_router.get("", response_model=Page[CreditNoteOut])
def list_credit_notes(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    customer_id: UUID | None = None,
    mobile: str | None = None,
    status_filter: CreditNoteStatus | None = Query(None, alias="status"),
    open_only: bool = False,
) -> Page[CreditNoteOut]:
    """Notes on file. `open_only` is what the counter offers as payment."""
    credit_notes.expire_lapsed(db, store_id=user.store_id)
    db.commit()

    query = db.query(CreditNote).filter(CreditNote.store_id == user.store_id)
    if customer_id is not None:
        query = query.filter(CreditNote.customer_id == customer_id)
    if mobile:
        cleaned = "".join(ch for ch in mobile if ch.isdigit())
        ids = [
            row[0]
            for row in db.query(Customer.id).filter(
                Customer.store_id == user.store_id, Customer.mobile == cleaned
            )
        ]
        query = query.filter(CreditNote.customer_id.in_(ids))
    if status_filter is not None:
        query = query.filter(CreditNote.status == status_filter.value)
    if open_only:
        query = query.filter(
            CreditNote.status.in_(
                [CreditNoteStatus.OPEN.value, CreditNoteStatus.PARTIALLY_USED.value]
            )
        )

    total = query.count()
    rows = (
        query.order_by(CreditNote.issue_date.desc(), CreditNote.id.desc())
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    return Page.build([_note_out(db, n) for n in rows], total, params)


@credit_notes_router.get("/{note_id}", response_model=CreditNoteOut)
def get_credit_note(note_id: UUID, user: CurrentUser, db: DbSession) -> CreditNoteOut:
    note = db.get(CreditNote, note_id)
    if note is None or note.store_id != user.store_id:
        raise NotFoundError("Credit note not found.")
    return _note_out(db, note, with_usages=True)


@credit_notes_router.post("/{note_id}/cancel", response_model=CreditNoteOut)
def cancel_credit_note(
    note_id: UUID, payload: CreditNoteCancelIn, request: Request, admin: AdminUser, db: DbSession
) -> CreditNoteOut:
    """Withdraw a note. Refused once any part of it has been spent."""
    note = db.get(CreditNote, note_id)
    if note is None or note.store_id != admin.store_id:
        raise NotFoundError("Credit note not found.")

    before = audit.snapshot(note, ["credit_note_no", "amount_issued", "balance_amount", "status"])
    credit_notes.cancel(db, note=note, reason=payload.reason, actor_id=admin.id)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="CreditNote", entity_id=note.id,
        user=admin, before=before,
        after=audit.snapshot(note, ["credit_note_no", "balance_amount", "status"]),
        reason=payload.reason, request=request,
    )
    db.commit()
    db.refresh(note)
    return _note_out(db, note, with_usages=True)


def _note_out(db, note: CreditNote, with_usages: bool = False) -> CreditNoteOut:
    customer = db.get(Customer, note.customer_id)
    usages = []
    if with_usages:
        for usage in (
            db.query(CreditNoteUsage)
            .filter(CreditNoteUsage.credit_note_id == note.id)
            .order_by(CreditNoteUsage.id)
        ):
            sale = db.get(Sale, usage.sale_id)
            usages.append(
                CreditNoteUsageOut(
                    id=usage.id,
                    sale_id=usage.sale_id,
                    invoice_no=sale.invoice_no if sale else None,
                    amount_used=usage.amount_used,
                    used_at=utc_iso(usage.used_at) or "",
                )
            )

    return CreditNoteOut(
        id=note.id,
        credit_note_no=note.credit_note_no,
        customer_id=note.customer_id,
        customer_mobile=customer.mobile if customer else None,
        customer_name=customer.name if customer else None,
        sale_return_id=note.sale_return_id,
        issue_date=note.issue_date,
        amount_issued=note.amount_issued,
        amount_used=note.amount_used,
        balance_amount=note.balance_amount,
        expires_on=note.expires_on,
        days_to_expiry=(note.expires_on - business_date()).days,
        status=note.status,
        notes=note.notes,
        usages=usages,
    )


# --- void workflow -------------------------------------------------------


@void_router.get("", response_model=Page[VoidRequestOut])
def list_void_requests(
    admin: AdminUser,
    db: DbSession,
    params: PageParams = Depends(),
    status_filter: ApprovalStatus | None = Query(ApprovalStatus.PENDING, alias="status"),
) -> Page[VoidRequestOut]:
    query = db.query(SaleVoidRequest).filter(SaleVoidRequest.store_id == admin.store_id)
    if status_filter is not None:
        query = query.filter(SaleVoidRequest.status == status_filter.value)

    total = query.count()
    rows = (
        query.order_by(SaleVoidRequest.requested_at.desc())
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    return Page.build([_void_out(db, r) for r in rows], total, params)


@void_router.post("/{request_id}/approve", response_model=VoidRequestOut)
def approve_void(
    request_id: UUID, payload: VoidReviewIn, request: Request, admin: AdminUser, db: DbSession
) -> VoidRequestOut:
    """Approve: every unit goes back to the batch it came from."""
    void_request = _load_void(db, request_id, admin.store_id)
    sale = voids.approve_void(
        db, request=void_request, note=payload.review_note, admin=admin
    )

    audit.record(
        db, action=AuditAction.APPROVE, entity_type="Sale", entity_id=sale.id, user=admin,
        after={"invoice_no": sale.invoice_no, "status": sale.status},
        reason=void_request.request_reason, request=request,
    )
    db.commit()
    db.refresh(void_request)
    return _void_out(db, void_request)


@void_router.post("/{request_id}/reject", response_model=VoidRequestOut)
def reject_void(
    request_id: UUID, payload: VoidRejectIn, request: Request, admin: AdminUser, db: DbSession
) -> VoidRequestOut:
    """Refuse: the invoice stands, and the refusal is on record."""
    void_request = _load_void(db, request_id, admin.store_id)
    sale = voids.reject_void(db, request=void_request, note=payload.review_note, admin=admin)

    audit.record(
        db, action=AuditAction.REJECT, entity_type="Sale",
        entity_id=sale.id if sale else None, user=admin,
        after={"status": sale.status if sale else None},
        reason=payload.review_note, request=request,
    )
    db.commit()
    db.refresh(void_request)
    return _void_out(db, void_request)


def _load_void(db, request_id: UUID, store_id: UUID) -> SaleVoidRequest:
    row = db.get(SaleVoidRequest, request_id)
    if row is None or row.store_id != store_id:
        raise NotFoundError("Void request not found.")
    return row


def _void_out(db, row: SaleVoidRequest) -> VoidRequestOut:
    sale = db.get(Sale, row.sale_id)
    return VoidRequestOut(
        id=row.id,
        sale_id=row.sale_id,
        invoice_no=sale.invoice_no if sale else None,
        invoice_date=sale.invoice_date if sale else None,
        net_amount=sale.net_amount if sale else None,
        customer_mobile=sale.customer_mobile_snapshot if sale else None,
        requested_by_id=row.requested_by_id,
        requested_by_name=row.requested_by_name,
        request_reason=row.request_reason,
        requested_at=utc_iso(row.requested_at) or "",
        status=row.status,
        reviewed_by_name=row.reviewed_by_name,
        review_note=row.review_note,
        reviewed_at=utc_iso(row.reviewed_at),
    )


# Requesting a void belongs with the sale it concerns; it is mounted on the
# sales router in api/v1/sales.py.
def request_void_for_sale(
    db, *, sale: Sale, payload: VoidRequestIn, user, request: Request
) -> VoidRequestOut:
    void_request = voids.request_void(
        db, sale=sale, reason=payload.reason, user=user
    )
    audit.record(
        db, action=AuditAction.VOID, entity_type="Sale", entity_id=sale.id, user=user,
        after={"invoice_no": sale.invoice_no, "status": sale.status},
        reason=payload.reason, request=request,
    )
    db.commit()
    db.refresh(void_request)
    return _void_out(db, void_request)
