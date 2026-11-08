"""Customer and doctor schemas.

Both are reachable by a sales executive: the counter has to be able to
record who bought what, and who prescribed it, without waiting for an admin.
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel


class CustomerBase(BaseModel):
    # The mobile number is the identity: it is what the counter types and
    # what the register and credit notes match on.
    mobile: str = Field(min_length=6, max_length=15)
    name: str | None = Field(default=None, max_length=150)
    address: str | None = Field(default=None, max_length=300)
    email: EmailStr | None = None
    date_of_birth: date | None = None
    gstin: str | None = Field(default=None, min_length=15, max_length=15)
    state_code: str | None = Field(default=None, min_length=1, max_length=2)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("mobile")
    @classmethod
    def _digits(cls, value: str) -> str:
        cleaned = "".join(ch for ch in value if ch.isdigit())
        if len(cleaned) < 6:
            raise ValueError("Enter a valid mobile number.")
        return cleaned


class CustomerCreate(CustomerBase):
    pass


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    address: str | None = Field(default=None, max_length=300)
    email: EmailStr | None = None
    date_of_birth: date | None = None
    gstin: str | None = Field(default=None, min_length=15, max_length=15)
    state_code: str | None = Field(default=None, min_length=1, max_length=2)
    notes: str | None = Field(default=None, max_length=1000)
    is_active: bool | None = None


class CustomerOut(ORMModel):
    id: UUID
    mobile: str
    name: str | None = None
    address: str | None = None
    email: str | None = None
    date_of_birth: date | None = None
    gstin: str | None = None
    state_code: str | None = None
    notes: str | None = None
    credit_balance: Decimal
    is_active: bool
    # Filled in on lookup and on the register so the counter can see history
    # at a glance. Voided invoices are excluded — they never happened.
    sale_count: int | None = None
    last_purchase_on: date | None = None
    total_spend: Decimal | None = None


class CustomerSaleRow(BaseModel):
    """One invoice in a customer's history."""

    id: UUID
    invoice_no: str
    invoice_date: date
    item_count: int
    net_amount: Decimal
    status: str


class CustomerCreditRow(BaseModel):
    """One credit note held by a customer."""

    id: UUID
    credit_note_no: str
    issue_date: date
    amount_issued: Decimal
    amount_used: Decimal
    balance_amount: Decimal
    expires_on: date
    days_to_expiry: int
    status: str


class CustomerDetail(CustomerOut):
    """Everything one customer's card shows.

    `credit_balance` is the cached column; `open_credit` is recomputed from
    the notes themselves, which are the truth. They should agree — showing
    both is what makes a drift visible instead of silent.
    """

    first_purchase_on: date | None = None
    open_credit: Decimal
    recent_sales: list[CustomerSaleRow] = []
    credit_notes: list[CustomerCreditRow] = []


class DoctorBase(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    registration_no: str | None = Field(default=None, max_length=50)
    specialization: str | None = Field(default=None, max_length=120)
    hospital_clinic: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=15)


class DoctorCreate(DoctorBase):
    pass


class DoctorUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    registration_no: str | None = Field(default=None, max_length=50)
    specialization: str | None = Field(default=None, max_length=120)
    hospital_clinic: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=15)
    is_active: bool | None = None


class DoctorOut(ORMModel):
    id: UUID
    name: str
    registration_no: str | None = None
    specialization: str | None = None
    hospital_clinic: str | None = None
    address: str | None = None
    phone: str | None = None
    is_active: bool
