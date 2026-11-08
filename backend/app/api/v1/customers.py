"""Customers and doctors.

Open to the counter, not just admins: a sales executive has to be able to
record a buyer and a prescriber mid-sale. Neither table carries money, so
there is nothing here to gate.
"""

from uuid import UUID

from datetime import datetime
from decimal import Decimal

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func

from app.core.deps import require_permission, CurrentUser, DbSession
from app.core.exceptions import ConflictError, NotFoundError
from app.models.enums import AuditAction, CreditNoteStatus, SaleStatus
from app.models.user import User
from app.models.party import Customer, Doctor
from app.models.returns import CreditNote
from app.models.sale import Sale, SaleItem
from app.schemas.catalog import DeleteResult
from app.schemas.common import Page, PageParams
from app.schemas.party import (
    CustomerCreate,
    CustomerCreditRow,
    CustomerDetail,
    CustomerOut,
    CustomerSaleRow,
    CustomerUpdate,
    DoctorCreate,
    DoctorOut,
    DoctorUpdate,
)
from app.services import audit
from app.utils.clock import business_date

ZERO = Decimal("0.00")

#: Admin *and* still holding Masters. The read routes above answer to the
#: counter as well (see api/v1/router.py); maintaining the data does not.
#: The counter adds and corrects customers and doctors all day. Removing
#: one is the exception, and it is off by default.
CustomerAuthor = Annotated[User, Depends(require_permission("customers:create"))]
CustomerEditor = Annotated[User, Depends(require_permission("customers:edit"))]
CustomerRemover = Annotated[User, Depends(require_permission("customers:delete"))]
DoctorAuthor = Annotated[User, Depends(require_permission("doctors:create"))]
DoctorEditor = Annotated[User, Depends(require_permission("doctors:edit"))]
DoctorRemover = Annotated[User, Depends(require_permission("doctors:delete"))]

customers_router = APIRouter()
doctors_router = APIRouter()


# --- customers -----------------------------------------------------------


def _history(db, customer_ids: list[UUID]) -> dict[UUID, tuple[int, object, object, Decimal]]:
    """Invoice count, first and last purchase, and lifetime spend, per customer.

    One grouped query for the whole page rather than one per row. A voided
    invoice is excluded everywhere: it was cancelled outright, so counting it
    would overstate both how often someone shops and what they have spent.
    """
    if not customer_ids:
        return {}
    rows = (
        db.query(
            Sale.customer_id,
            func.count(Sale.id),
            func.min(Sale.invoice_date),
            func.max(Sale.invoice_date),
            func.sum(Sale.net_amount),
        )
        .filter(
            Sale.customer_id.in_(customer_ids),
            Sale.status != SaleStatus.VOIDED.value,
        )
        .group_by(Sale.customer_id)
        .all()
    )
    return {r[0]: (r[1] or 0, r[2], r[3], r[4] or ZERO) for r in rows}


def _customer_out(db, customer: Customer, with_history: bool = False) -> CustomerOut:
    out = CustomerOut.model_validate(customer)
    if with_history:
        count, _first, last, spend = _history(db, [customer.id]).get(
            customer.id, (0, None, None, ZERO)
        )
        out.sale_count, out.last_purchase_on, out.total_spend = count, last, spend
    return out


@customers_router.get("", response_model=Page[CustomerOut])
def list_customers(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = Query(None, description="Mobile number or name"),
    is_active: bool | None = None,
    has_credit: bool = False,
    sort: str = Query("name", pattern="^(name|-name|mobile|-created_at|-credit_balance)$"),
) -> Page[CustomerOut]:
    """The customer register. Each row carries its own purchase history.

    Open to the counter: recognising a returning customer, and seeing what
    credit they are holding, is counter work — there is no cost figure here
    to gate.
    """
    query = db.query(Customer).filter(Customer.store_id == user.store_id)
    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.filter(
            Customer.mobile.like(needle) | func.lower(Customer.name).like(needle)
        )
    if is_active is not None:
        query = query.filter(Customer.is_active.is_(is_active))
    if has_credit:
        # A cached balance can only be above zero if a note is open, so this
        # filter needs no join.
        query = query.filter(Customer.credit_balance > 0)

    # A customer entered mid-sale often has no name yet. `coalesce` keeps the
    # unnamed ones together at one end on both SQLite and PostgreSQL, which
    # disagree about where a NULL sorts.
    named = func.coalesce(Customer.name, "")
    order = {
        "name": (named, Customer.mobile),
        "-name": (named.desc(), Customer.mobile),
        "mobile": (Customer.mobile,),
        "-created_at": (Customer.created_at.desc(), Customer.id.desc()),
        "-credit_balance": (Customer.credit_balance.desc(), named),
    }[sort]

    total = query.count()
    rows = query.order_by(*order).offset(params.offset).limit(params.page_size).all()

    history = _history(db, [r.id for r in rows])
    items = []
    for row in rows:
        out = CustomerOut.model_validate(row)
        count, _first, last, spend = history.get(row.id, (0, None, None, ZERO))
        out.sale_count, out.last_purchase_on, out.total_spend = count, last, spend
        items.append(out)
    return Page.build(items, total, params)


@customers_router.get("/by-mobile/{mobile}", response_model=CustomerOut)
def customer_by_mobile(mobile: str, user: CurrentUser, db: DbSession) -> CustomerOut:
    """The counter's lookup: type a mobile, get the customer and their history."""
    cleaned = "".join(ch for ch in mobile if ch.isdigit())
    customer = (
        db.query(Customer)
        .filter(Customer.store_id == user.store_id, Customer.mobile == cleaned)
        .one_or_none()
    )
    if customer is None:
        raise NotFoundError("No customer with that mobile number.", field="mobile")
    return _customer_out(db, customer, with_history=True)


@customers_router.get("/{customer_id}", response_model=CustomerDetail)
def get_customer(
    customer_id: UUID,
    user: CurrentUser,
    db: DbSession,
    sales_limit: int = Query(20, ge=1, le=100),
) -> CustomerDetail:
    """One customer's card: who they are, what they have bought, what credit
    they hold."""
    customer = db.get(Customer, customer_id)
    if customer is None or customer.store_id != user.store_id:
        raise NotFoundError("Customer not found.")

    count, first, last, spend = _history(db, [customer.id]).get(
        customer.id, (0, None, None, ZERO)
    )

    recent = (
        db.query(Sale)
        .filter(Sale.customer_id == customer.id)
        .order_by(Sale.invoice_datetime.desc(), Sale.id.desc())
        .limit(sales_limit)
        .all()
    )
    counts = dict(
        db.query(SaleItem.sale_id, func.count(SaleItem.id))
        .filter(SaleItem.sale_id.in_([s.id for s in recent]))
        .group_by(SaleItem.sale_id)
        .all()
    )

    today = business_date()
    notes = (
        db.query(CreditNote)
        .filter(
            CreditNote.store_id == user.store_id,
            CreditNote.customer_id == customer.id,
        )
        .order_by(CreditNote.issue_date.desc(), CreditNote.id.desc())
        .all()
    )
    spendable = [
        n
        for n in notes
        if n.status
        in (CreditNoteStatus.OPEN.value, CreditNoteStatus.PARTIALLY_USED.value)
        and n.balance_amount > ZERO
        and n.expires_on >= today
    ]

    return CustomerDetail(
        **CustomerOut.model_validate(customer).model_dump(
            exclude={"sale_count", "last_purchase_on", "total_spend"}
        ),
        sale_count=count,
        last_purchase_on=last,
        first_purchase_on=first,
        total_spend=spend,
        open_credit=sum((n.balance_amount for n in spendable), ZERO),
        recent_sales=[
            CustomerSaleRow(
                id=s.id,
                invoice_no=s.invoice_no,
                invoice_date=s.invoice_date,
                item_count=counts.get(s.id, 0),
                net_amount=s.net_amount,
                status=s.status,
            )
            for s in recent
        ],
        credit_notes=[
            CustomerCreditRow(
                id=n.id,
                credit_note_no=n.credit_note_no,
                issue_date=n.issue_date,
                amount_issued=n.amount_issued,
                amount_used=n.amount_used,
                balance_amount=n.balance_amount,
                expires_on=n.expires_on,
                days_to_expiry=(n.expires_on - today).days,
                status=n.status,
            )
            for n in notes
        ],
    )


@customers_router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate, request: Request, user: CustomerAuthor, db: DbSession
) -> CustomerOut:
    exists = (
        db.query(func.count(Customer.id))
        .filter(Customer.store_id == user.store_id, Customer.mobile == payload.mobile)
        .scalar()
    )
    if exists:
        raise ConflictError(
            "A customer with that mobile number already exists.",
            code="MOBILE_ALREADY_EXISTS",
            field="mobile",
        )

    customer = Customer(
        store_id=user.store_id,
        **payload.model_dump(exclude={"email"}),
        email=str(payload.email) if payload.email else None,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(customer)
    db.flush()
    audit.record(
        db, action=AuditAction.CREATE, entity_type="Customer", entity_id=customer.id,
        user=user, after=audit.snapshot(customer), request=request,
    )
    db.commit()
    db.refresh(customer)
    return CustomerOut.model_validate(customer)


@customers_router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: UUID, payload: CustomerUpdate, request: Request, user: CustomerEditor,
    db: DbSession
) -> CustomerOut:
    customer = db.get(Customer, customer_id)
    if customer is None or customer.store_id != user.store_id:
        raise NotFoundError("Customer not found.")

    before = audit.snapshot(customer)
    changes = payload.model_dump(exclude_unset=True)
    if "email" in changes and changes["email"] is not None:
        changes["email"] = str(changes["email"])
    for field, value in changes.items():
        setattr(customer, field, value)
    customer.updated_by_id = user.id

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Customer", entity_id=customer.id,
        user=user, before=before, after=audit.snapshot(customer), request=request,
    )
    db.commit()
    db.refresh(customer)
    return _customer_out(db, customer, with_history=True)


@customers_router.delete("/{customer_id}", response_model=DeleteResult)
def delete_customer(
    customer_id: UUID, request: Request, admin: CustomerRemover, db: DbSession
) -> DeleteResult:
    """Deletes a customer entered in error; deactivates one who has bought."""
    customer = db.get(Customer, customer_id)
    if customer is None or customer.store_id != admin.store_id:
        raise NotFoundError("Customer not found.")

    before = audit.snapshot(customer)
    sales = db.query(func.count(Sale.id)).filter(Sale.customer_id == customer.id).scalar()
    if sales:
        if not customer.is_active:
            raise ConflictError(
                f"This customer has {sales} invoice(s) and cannot be deleted.",
                code="CUSTOMER_IN_USE",
            )
        customer.is_active = False
        customer.updated_by_id = admin.id
        audit.record(
            db, action=AuditAction.UPDATE, entity_type="Customer", entity_id=customer.id,
            user=admin, before=before, after=audit.snapshot(customer),
            reason="Deactivated instead of deleted — invoices exist.", request=request,
        )
        db.commit()
        return DeleteResult(
            deleted=False,
            id=customer_id,
            message=(
                f"This customer has {sales} invoice(s), so the record was deactivated "
                f"rather than deleted. The invoices are untouched."
            ),
        )

    db.delete(customer)
    audit.record(
        db, action=AuditAction.DELETE, entity_type="Customer", entity_id=customer_id,
        user=admin, before=before, request=request,
    )
    db.commit()
    return DeleteResult(deleted=True, id=customer_id, message="Customer deleted.")


# --- doctors -------------------------------------------------------------


@doctors_router.get("", response_model=Page[DoctorOut])
def list_doctors(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    is_active: bool | None = None,
) -> Page[DoctorOut]:
    query = db.query(Doctor).filter(Doctor.store_id == user.store_id)
    if q:
        needle = f"%{q.strip().lower()}%"
        query = query.filter(
            func.lower(Doctor.name).like(needle)
            | func.lower(Doctor.hospital_clinic).like(needle)
        )
    if is_active is not None:
        query = query.filter(Doctor.is_active.is_(is_active))

    total = query.count()
    rows = query.order_by(Doctor.name).offset(params.offset).limit(params.page_size).all()
    return Page.build([DoctorOut.model_validate(r) for r in rows], total, params)


@doctors_router.post("", response_model=DoctorOut, status_code=status.HTTP_201_CREATED)
def create_doctor(
    payload: DoctorCreate, request: Request, user: DoctorAuthor, db: DbSession
) -> DoctorOut:
    name = payload.name.strip()
    clash = (
        db.query(func.count(Doctor.id))
        .filter(
            Doctor.store_id == user.store_id,
            func.lower(Doctor.name) == name.lower(),
            Doctor.hospital_clinic == payload.hospital_clinic,
        )
        .scalar()
    )
    if clash:
        raise ConflictError(
            "That doctor is already on file.", code="DOCTOR_EXISTS", field="name"
        )

    doctor = Doctor(
        store_id=user.store_id,
        **payload.model_dump(exclude={"name"}),
        name=name,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(doctor)
    db.flush()
    audit.record(
        db, action=AuditAction.CREATE, entity_type="Doctor", entity_id=doctor.id,
        user=user, after=audit.snapshot(doctor), request=request,
    )
    db.commit()
    db.refresh(doctor)
    return DoctorOut.model_validate(doctor)


@doctors_router.patch("/{doctor_id}", response_model=DoctorOut)
def update_doctor(
    doctor_id: UUID, payload: DoctorUpdate, request: Request, user: DoctorEditor, db: DbSession
) -> DoctorOut:
    doctor = db.get(Doctor, doctor_id)
    if doctor is None or doctor.store_id != user.store_id:
        raise NotFoundError("Doctor not found.")

    before = audit.snapshot(doctor)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(doctor, field, value)
    doctor.updated_by_id = user.id

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Doctor", entity_id=doctor.id,
        user=user, before=before, after=audit.snapshot(doctor), request=request,
    )
    db.commit()
    db.refresh(doctor)
    return DoctorOut.model_validate(doctor)


@doctors_router.delete("/{doctor_id}", response_model=DeleteResult)
def delete_doctor(
    doctor_id: UUID, request: Request, admin: DoctorRemover, db: DbSession
) -> DeleteResult:
    """Deletes a doctor entered in error; deactivates one named on an invoice."""
    doctor = db.get(Doctor, doctor_id)
    if doctor is None or doctor.store_id != admin.store_id:
        raise NotFoundError("Doctor not found.")

    before = audit.snapshot(doctor)
    prescriptions = db.query(func.count(Sale.id)).filter(Sale.doctor_id == doctor.id).scalar()
    if prescriptions:
        if not doctor.is_active:
            raise ConflictError(
                f"{doctor.name} appears on {prescriptions} invoice(s) and cannot be deleted.",
                code="DOCTOR_IN_USE",
            )
        doctor.is_active = False
        doctor.updated_by_id = admin.id
        audit.record(
            db, action=AuditAction.UPDATE, entity_type="Doctor", entity_id=doctor.id,
            user=admin, before=before, after=audit.snapshot(doctor),
            reason="Deactivated instead of deleted — invoices name this doctor.",
            request=request,
        )
        db.commit()
        return DeleteResult(
            deleted=False,
            id=doctor_id,
            message=(
                f"{doctor.name} appears on {prescriptions} invoice(s), so the record was "
                f"deactivated rather than deleted."
            ),
        )

    db.delete(doctor)
    audit.record(
        db, action=AuditAction.DELETE, entity_type="Doctor", entity_id=doctor_id,
        user=admin, before=before, request=request,
    )
    db.commit()
    return DeleteResult(deleted=True, id=doctor_id, message="Doctor deleted.")
