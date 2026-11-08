"""Debit notes — goods back to a distributor, and the credit that creates.

Every figure on one of these is derived from cost, so the whole module sits
behind the Purchases capabilities and the responses are cost-free for anyone
holding them without admin (CLAUDE.md rule 4).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.core.deps import CurrentSettings, DbSession, require_permission
from app.core.exceptions import ConflictError, NotFoundError
from app.models.enums import NO_EXPIRY, AuditAction, PurchaseReturnStatus
from app.models.party import Supplier
from app.models.purchase import PurchaseInvoice
from app.models.purchase_return import PurchaseReturn, SupplierCreditUsage
from app.models.user import User
from app.schemas.common import Page, PageParams
from app.schemas.purchase_return import (
    CreditApply,
    PurchaseReturnCancel,
    PurchaseReturnCreate,
    PurchaseReturnOut,
    PurchaseReturnOutPublic,
    PurchaseReturnRow,
    PurchaseReturnRowPublic,
    SupplierCreditRow,
)
from app.services import audit, purchase_returns
from app.utils.clock import utc_iso

#: Reading a debit note is part of reading the buying side.
ReturnReader = Annotated[
    User, Depends(require_permission("purchases:read", "purchases:return"))
]
#: Raising or cancelling one moves stock and creates money.
ReturnWriter = Annotated[User, Depends(require_permission("purchases:return"))]

router = APIRouter()

AUDIT_FIELDS = [
    "id", "debit_note_no", "supplier_id", "purchase_invoice_id", "return_date",
    "taxable_amount", "cgst_amount", "sgst_amount", "igst_amount", "total_amount",
    "credit_used", "credit_balance", "credit_status", "status",
]


def _item_out(item, *, as_admin: bool) -> dict:
    common = {
        "id": item.id,
        "line_no": item.line_no,
        "medicine_id": item.medicine_id,
        "medicine_name": item.medicine_name_snapshot,
        "pack_label": item.pack_label_snapshot,
        "hsn_code": item.hsn_code_snapshot,
        "batch_id": item.batch_id,
        "batch_no": item.batch_no_snapshot,
        "expiry_date": item.expiry_date_snapshot,
        # Goods with no shelf life carry the sentinel; say so rather than
        # letting a document print the year 9999.
        "has_expiry": item.expiry_date_snapshot != NO_EXPIRY,
        "qty_units": item.qty_units,
    }
    if not as_admin:
        return common
    return {
        **common,
        "cost_per_unit": item.cost_per_unit,
        "gst_rate": item.gst_rate,
        "taxable_value": item.taxable_value,
        "cgst_amount": item.cgst_amount,
        "sgst_amount": item.sgst_amount,
        "igst_amount": item.igst_amount,
        "line_total": item.line_total,
    }


def _row(document: PurchaseReturn, *, as_admin: bool, supplier_name: str | None) -> dict:
    common = {
        "id": document.id,
        "debit_note_no": document.debit_note_no,
        "supplier_id": document.supplier_id,
        "supplier_name": supplier_name,
        "return_date": document.return_date,
        "reason": document.reason,
        "supplier_credit_note_ref": document.supplier_credit_note_ref,
        "status": document.status,
        "item_count": len(document.items),
    }
    if not as_admin:
        return common
    return {
        **common,
        "is_intra_state": document.is_intra_state,
        "taxable_amount": document.taxable_amount,
        "cgst_amount": document.cgst_amount,
        "sgst_amount": document.sgst_amount,
        "igst_amount": document.igst_amount,
        "total_amount": document.total_amount,
        "credit_used": document.credit_used,
        "credit_balance": document.credit_balance,
        "credit_status": document.credit_status,
    }


def _detail(document: PurchaseReturn, *, as_admin: bool, supplier_name: str | None):
    payload = {
        **_row(document, as_admin=as_admin, supplier_name=supplier_name),
        "purchase_invoice_id": document.purchase_invoice_id,
        "notes": document.notes,
        "cancel_reason": document.cancel_reason,
        "items": [_item_out(i, as_admin=as_admin) for i in document.items],
    }
    model = PurchaseReturnOut if as_admin else PurchaseReturnOutPublic
    return model(**payload)


def _loaded(query):
    return query.options(selectinload(PurchaseReturn.items))


def _supplier_names(db, documents) -> dict:
    ids = {d.supplier_id for d in documents}
    if not ids:
        return {}
    return dict(db.query(Supplier.id, Supplier.name).filter(Supplier.id.in_(ids)).all())


# --- raising one ----------------------------------------------------------


@router.post(
    "",
    response_model=PurchaseReturnOut | PurchaseReturnOutPublic,
    status_code=status.HTTP_201_CREATED,
)
def create_purchase_return(
    payload: PurchaseReturnCreate,
    request: Request,
    user: ReturnWriter,
    settings: CurrentSettings,
    db: DbSession,
):
    """Send stock back to a distributor.

    Stock leaves through the ledger, the input tax claimed on it is reversed
    at the cost and rate it arrived at, and the value becomes credit against
    that distributor — all in one transaction (CLAUDE.md rule 7).
    """
    document = purchase_returns.create_return(
        db, store_id=user.store_id, payload=payload, settings=settings, user=user
    )

    audit.record(
        db, action=AuditAction.CREATE, entity_type="PurchaseReturn", entity_id=document.id,
        user=user, before=None, after=audit.snapshot(document, AUDIT_FIELDS),
        reason=document.reason, request=request,
    )
    db.commit()
    db.refresh(document)

    supplier = db.get(Supplier, document.supplier_id)
    return _detail(document, as_admin=user.is_admin, supplier_name=supplier.name)


@router.post(
    "/{purchase_return_id}/cancel",
    response_model=PurchaseReturnOut | PurchaseReturnOutPublic,
)
def cancel_purchase_return(
    purchase_return_id: UUID,
    payload: PurchaseReturnCancel,
    request: Request,
    user: ReturnWriter,
    db: DbSession,
):
    """Put the goods back and withdraw the credit.

    Refused once any of the credit has been set against an invoice.
    """
    before = None
    existing = db.get(PurchaseReturn, purchase_return_id)
    if existing is not None and existing.store_id == user.store_id:
        before = audit.snapshot(existing, AUDIT_FIELDS)

    document = purchase_returns.cancel_return(
        db,
        store_id=user.store_id,
        purchase_return_id=purchase_return_id,
        reason=payload.reason.strip(),
        user=user,
    )

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="PurchaseReturn", entity_id=document.id,
        user=user, before=before, after=audit.snapshot(document, AUDIT_FIELDS),
        reason=payload.reason, request=request,
    )
    db.commit()
    db.refresh(document)

    supplier = db.get(Supplier, document.supplier_id)
    return _detail(document, as_admin=user.is_admin, supplier_name=supplier.name)


# --- reading them ---------------------------------------------------------


@router.get("", response_model=Page[PurchaseReturnRow | PurchaseReturnRowPublic])
def list_purchase_returns(
    user: ReturnReader,
    db: DbSession,
    page: PageParams = Depends(),
    supplier_id: UUID | None = None,
    status_filter: PurchaseReturnStatus | None = Query(None, alias="status"),
):
    query = _loaded(
        db.query(PurchaseReturn).filter(PurchaseReturn.store_id == user.store_id)
    )
    if supplier_id is not None:
        query = query.filter(PurchaseReturn.supplier_id == supplier_id)
    if status_filter is not None:
        query = query.filter(PurchaseReturn.status == status_filter.value)

    total = query.with_entities(func.count(PurchaseReturn.id)).scalar() or 0
    rows = (
        query.order_by(PurchaseReturn.return_date.desc(), PurchaseReturn.debit_note_no.desc())
        .offset((page.page - 1) * page.page_size)
        .limit(page.page_size)
        .all()
    )
    names = _supplier_names(db, rows)
    model = PurchaseReturnRow if user.is_admin else PurchaseReturnRowPublic
    return Page(
        items=[
            model(**_row(r, as_admin=user.is_admin, supplier_name=names.get(r.supplier_id)))
            for r in rows
        ],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get("/credit", response_model=list[SupplierCreditRow])
def supplier_credit(
    supplier_id: UUID,
    user: ReturnReader,
    db: DbSession,
) -> list[SupplierCreditRow]:
    """What this distributor still owes, oldest first.

    Admin-only in substance: the figures are cost. A non-admin holding
    `purchases:return` gets an empty list rather than a 403, because the
    purchase screen asks for this on every supplier change and a hard error
    there would read as a broken page.
    """
    if not user.is_admin:
        return []
    return [
        SupplierCreditRow(
            purchase_return_id=d.id,
            debit_note_no=d.debit_note_no,
            return_date=d.return_date,
            total_amount=d.total_amount,
            credit_used=d.credit_used,
            credit_balance=d.credit_balance,
            credit_status=d.credit_status,
        )
        for d in purchase_returns.available_credit(db, user.store_id, supplier_id)
    ]


@router.get(
    "/{purchase_return_id}", response_model=PurchaseReturnOut | PurchaseReturnOutPublic
)
def read_purchase_return(purchase_return_id: UUID, user: ReturnReader, db: DbSession):
    document = (
        _loaded(db.query(PurchaseReturn))
        .filter(
            PurchaseReturn.id == purchase_return_id,
            PurchaseReturn.store_id == user.store_id,
        )
        .one_or_none()
    )
    if document is None:
        raise NotFoundError("Debit note not found.")
    supplier = db.get(Supplier, document.supplier_id)
    return _detail(document, as_admin=user.is_admin, supplier_name=supplier.name)


# --- spending the credit --------------------------------------------------


@router.post("/apply-to-invoice/{purchase_invoice_id}", status_code=status.HTTP_201_CREATED)
def apply_credit_to_invoice(
    purchase_invoice_id: UUID,
    payload: CreditApply,
    request: Request,
    user: ReturnWriter,
    db: DbSession,
) -> dict:
    """Set part of a debit note against a purchase invoice.

    A settlement, not a discount: the invoice's taxable value and its input
    tax credit are untouched. What changes is how much is left to pay.
    """
    invoice = (
        db.query(PurchaseInvoice)
        .filter(
            PurchaseInvoice.id == purchase_invoice_id,
            PurchaseInvoice.store_id == user.store_id,
        )
        .one_or_none()
    )
    if invoice is None:
        raise NotFoundError("Purchase invoice not found.")
    if invoice.is_cancelled:
        raise ConflictError(
            f"Invoice {invoice.doc_number} was cancelled; there is nothing to settle.",
            code="INVOICE_CANCELLED",
        )

    usage = purchase_returns.apply_credit(
        db,
        store_id=user.store_id,
        purchase_return_id=payload.purchase_return_id,
        invoice=invoice,
        amount=payload.amount,
        user=user,
    )

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="PurchaseInvoice", entity_id=invoice.id,
        user=user, before=None,
        after={"credit_applied": str(usage.amount_used),
               "purchase_return_id": str(payload.purchase_return_id)},
        reason="Supplier credit applied", request=request,
    )
    db.commit()

    applied = purchase_returns.credit_applied_to(db, invoice.id)
    return {
        "purchase_invoice_id": str(invoice.id),
        "amount_used": str(usage.amount_used),
        "used_at": utc_iso(usage.used_at),
        "total_credit_applied": str(applied),
        "invoice_total": str(invoice.total_amount),
        "balance_payable": str(invoice.total_amount - applied),
    }


@router.get("/applied/{purchase_invoice_id}")
def credit_on_invoice(
    purchase_invoice_id: UUID, user: ReturnReader, db: DbSession
) -> dict:
    """Credit already set against one invoice, and what is left to pay."""
    invoice = (
        db.query(PurchaseInvoice)
        .filter(
            PurchaseInvoice.id == purchase_invoice_id,
            PurchaseInvoice.store_id == user.store_id,
        )
        .one_or_none()
    )
    if invoice is None:
        raise NotFoundError("Purchase invoice not found.")
    if not user.is_admin:
        return {"purchase_invoice_id": str(invoice.id), "usages": []}

    rows = (
        db.query(SupplierCreditUsage, PurchaseReturn.debit_note_no)
        .join(PurchaseReturn, SupplierCreditUsage.purchase_return_id == PurchaseReturn.id)
        .filter(SupplierCreditUsage.purchase_invoice_id == invoice.id)
        .order_by(SupplierCreditUsage.used_at)
        .all()
    )
    applied = purchase_returns.credit_applied_to(db, invoice.id)
    return {
        "purchase_invoice_id": str(invoice.id),
        "invoice_total": str(invoice.total_amount),
        "total_credit_applied": str(applied),
        "balance_payable": str(invoice.total_amount - applied),
        "usages": [
            {
                "id": str(u.id),
                "purchase_return_id": str(u.purchase_return_id),
                "debit_note_no": note_no,
                "amount_used": str(u.amount_used),
                "used_at": utc_iso(u.used_at),
            }
            for u, note_no in rows
        ],
    }
