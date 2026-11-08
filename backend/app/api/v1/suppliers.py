"""Suppliers — the distributors goods are bought from.

Admin-only throughout: who the shop buys from, on what terms, is owner
information, and it is the doorway to cost price.
"""

from uuid import UUID

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import func

from app.core.deps import CurrentUser, DbSession, require_permission
from app.core.exceptions import ConflictError, NotFoundError
from app.models.enums import AuditAction
from app.models.user import User
from app.models.party import Supplier
from app.models.purchase import PurchaseInvoice
from app.schemas.catalog import DeleteResult
from app.schemas.common import Page, PageParams
from app.schemas.purchase import SupplierCreate, SupplierOut, SupplierUpdate
from app.services import audit

#: Maintaining the supplier list. Reading it is shared with goods inward
#: (see api/v1/router.py) — the purchase form has to offer the choice.
#: Holding the Suppliers module. A distributor's name, GSTIN and contact
#: details carry nothing commercial, so this one delegates whole — there
#: is no role check behind the module.
#: Maintaining the distributor list. Reading it is shared with goods
#: inward (see api/v1/router.py) — the purchase form has to offer the
#: choice.
SupplierKeeper = Annotated[User, Depends(require_permission("suppliers:manage"))]

router = APIRouter()


def _out(supplier: Supplier, purchase_count: int | None = None) -> SupplierOut:
    return SupplierOut(
        id=supplier.id,
        name=supplier.name,
        gstin=supplier.gstin,
        state_code=supplier.state_code,
        address=supplier.address,
        phone=supplier.phone,
        email=supplier.email,
        contact_person=supplier.contact_person,
        drug_licence_no=supplier.drug_licence_no,
        payment_terms_days=supplier.payment_terms_days,
        is_active=supplier.is_active,
        purchase_count=purchase_count,
    )


@router.get("", response_model=Page[SupplierOut])
def list_suppliers(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    is_active: bool | None = None,
) -> Page[SupplierOut]:
    query = db.query(Supplier).filter(Supplier.store_id == user.store_id)
    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.filter(
            func.lower(Supplier.name).like(needle) | func.lower(Supplier.gstin).like(needle)
        )
    if is_active is not None:
        query = query.filter(Supplier.is_active.is_(is_active))

    total = query.count()
    rows = query.order_by(Supplier.name).offset(params.offset).limit(params.page_size).all()

    counts = dict(
        db.query(PurchaseInvoice.supplier_id, func.count(PurchaseInvoice.id))
        .filter(PurchaseInvoice.supplier_id.in_([r.id for r in rows]))
        .group_by(PurchaseInvoice.supplier_id)
        .all()
    )
    return Page.build([_out(r, counts.get(r.id, 0)) for r in rows], total, params)


@router.get("/{supplier_id}", response_model=SupplierOut)
def get_supplier(supplier_id: UUID, user: CurrentUser, db: DbSession) -> SupplierOut:
    supplier = _load(db, supplier_id, user.store_id)
    count = (
        db.query(func.count(PurchaseInvoice.id))
        .filter(PurchaseInvoice.supplier_id == supplier.id)
        .scalar()
    )
    return _out(supplier, count)


@router.post("", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreate, request: Request, user: SupplierKeeper, db: DbSession
) -> SupplierOut:
    name = payload.name.strip()
    if (
        db.query(func.count(Supplier.id))
        .filter(Supplier.store_id == user.store_id, func.lower(Supplier.name) == name.lower())
        .scalar()
    ):
        raise ConflictError(
            "That supplier already exists.", code="SUPPLIER_EXISTS", field="name"
        )

    supplier = Supplier(
        store_id=user.store_id,
        **payload.model_dump(exclude={"name", "email"}),
        name=name,
        email=str(payload.email) if payload.email else None,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(supplier)
    db.flush()
    audit.record(
        db, action=AuditAction.CREATE, entity_type="Supplier", entity_id=supplier.id,
        user=user, after=audit.snapshot(supplier), request=request,
    )
    db.commit()
    db.refresh(supplier)
    return _out(supplier, 0)


@router.patch("/{supplier_id}", response_model=SupplierOut)
def update_supplier(
    supplier_id: UUID, payload: SupplierUpdate, request: Request, user: SupplierKeeper, db: DbSession
) -> SupplierOut:
    supplier = _load(db, supplier_id, user.store_id)
    before = audit.snapshot(supplier)

    changes = payload.model_dump(exclude_unset=True)
    if changes.get("name"):
        changes["name"] = changes["name"].strip()
        clash = (
            db.query(func.count(Supplier.id))
            .filter(
                Supplier.store_id == user.store_id,
                func.lower(Supplier.name) == changes["name"].lower(),
                Supplier.id != supplier.id,
            )
            .scalar()
        )
        if clash:
            raise ConflictError(
                "Another supplier already uses that name.",
                code="SUPPLIER_EXISTS",
                field="name",
            )
    if "email" in changes and changes["email"] is not None:
        changes["email"] = str(changes["email"])

    for field, value in changes.items():
        setattr(supplier, field, value)
    supplier.updated_by_id = user.id

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Supplier", entity_id=supplier.id,
        user=user, before=before, after=audit.snapshot(supplier), request=request,
    )
    db.commit()
    db.refresh(supplier)
    return _out(supplier)


@router.delete("/{supplier_id}", response_model=DeleteResult)
def delete_supplier(
    supplier_id: UUID, request: Request, user: SupplierKeeper, db: DbSession
) -> DeleteResult:
    """Deletes a supplier entered in error; deactivates one that has traded.

    A supplier with purchases behind it is referenced by the GST inward
    register and by every batch it supplied, so it is retired rather than
    removed.
    """
    supplier = _load(db, supplier_id, user.store_id)
    before = audit.snapshot(supplier)

    purchases = (
        db.query(func.count(PurchaseInvoice.id))
        .filter(PurchaseInvoice.supplier_id == supplier.id)
        .scalar()
    )
    if purchases:
        if not supplier.is_active:
            raise ConflictError(
                f"{supplier.name} has {purchases} purchase invoice(s) and cannot be deleted. "
                f"It is already inactive.",
                code="SUPPLIER_IN_USE",
            )
        supplier.is_active = False
        supplier.updated_by_id = user.id
        audit.record(
            db, action=AuditAction.UPDATE, entity_type="Supplier", entity_id=supplier.id,
            user=user, before=before, after=audit.snapshot(supplier),
            reason="Deactivated instead of deleted — purchase history exists.",
            request=request,
        )
        db.commit()
        return DeleteResult(
            deleted=False,
            id=supplier_id,
            message=(
                f"{supplier.name} has {purchases} purchase invoice(s), so it was "
                f"deactivated rather than deleted. It no longer appears when receiving "
                f"goods, and its invoices stay intact."
            ),
        )

    db.delete(supplier)
    audit.record(
        db, action=AuditAction.DELETE, entity_type="Supplier", entity_id=supplier_id,
        user=user, before=before, request=request,
    )
    db.commit()
    return DeleteResult(deleted=True, id=supplier_id, message="Supplier deleted.")


def _load(db, supplier_id: UUID, store_id: UUID) -> Supplier:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.store_id != store_id:
        raise NotFoundError("Supplier not found.")
    return supplier
