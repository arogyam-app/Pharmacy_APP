"""Void, return and credit-note schemas."""

from uuid import UUID

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import (
    ApprovalStatus,
    CreditNoteStatus,
    ItemCondition,
    RefundMode,
    ReturnStatus,
)
from app.schemas.common import ORMModel


# --- voids ---------------------------------------------------------------


class VoidRequestIn(BaseModel):
    # Required: a void with no stated reason is indistinguishable from a
    # till being balanced after the fact.
    reason: str = Field(min_length=5, max_length=500)


class VoidReviewIn(BaseModel):
    review_note: str | None = Field(default=None, max_length=500)


class VoidRejectIn(BaseModel):
    review_note: str = Field(min_length=3, max_length=500)


class VoidRequestOut(ORMModel):
    id: UUID
    sale_id: UUID
    invoice_no: str | None = None
    invoice_date: date | None = None
    net_amount: Decimal | None = None
    customer_mobile: str | None = None
    requested_by_id: UUID
    requested_by_name: str | None = None
    request_reason: str
    requested_at: str
    status: ApprovalStatus
    reviewed_by_name: str | None = None
    review_note: str | None = None
    reviewed_at: str | None = None


# --- returns -------------------------------------------------------------


class ReturnableLine(BaseModel):
    """One sale line and how much of it can still come back."""

    sale_item_id: UUID
    line_no: int
    medicine_id: UUID
    medicine_name: str
    batch_id: UUID
    batch_no: str
    expiry_date: date
    qty_sold_units: Decimal
    qty_returned_units: Decimal
    qty_returnable_units: Decimal
    unit_mrp: Decimal
    discount_pct: Decimal
    gst_rate: Decimal
    refund_if_all: Decimal


class ReturnableItems(BaseModel):
    sale_id: UUID
    invoice_no: str
    invoice_date: date
    customer_mobile: str | None = None
    customer_id: UUID | None = None
    days_since_sale: int
    return_window_days: int
    within_window: bool
    # False when the invoice is void or everything has already come back.
    can_return: bool
    items: list[ReturnableLine]


class ReturnItemIn(BaseModel):
    sale_item_id: UUID
    qty_units: Decimal = Field(gt=0, le=100000)
    # Damaged and expired goods are refunded but never put back on sale.
    condition: ItemCondition = ItemCondition.SALEABLE


class ReturnCreate(BaseModel):
    sale_id: UUID
    items: list[ReturnItemIn] = Field(min_length=1)
    refund_mode: RefundMode = RefundMode.CREDIT_NOTE
    reason: str | None = Field(default=None, max_length=500)


class ReturnItemOut(ORMModel):
    id: UUID
    sale_item_id: UUID
    medicine_id: UUID
    medicine_name: str
    batch_id: UUID
    batch_no: str
    qty_units: Decimal
    unit_mrp: Decimal
    discount_pct: Decimal
    gst_rate: Decimal
    taxable_value: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    refund_amount: Decimal
    condition: ItemCondition
    restocked: bool


class ReturnRow(BaseModel):
    id: UUID
    return_no: str
    return_date: date
    sale_id: UUID
    invoice_no: str | None = None
    customer_mobile: str | None = None
    refund_mode: RefundMode
    total_refund_amount: Decimal
    status: ReturnStatus
    item_count: int = 0
    credit_note_no: str | None = None


class ReturnOut(ReturnRow):
    reason: str | None = None
    gross_amount: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    processed_by_name: str | None = None
    approved_by_name: str | None = None
    days_since_sale: int
    restocked: bool
    items: list[ReturnItemOut] = []
    credit_note_id: UUID | None = None
    # Stock not put back, an out-of-window return — worth saying out loud.
    warnings: list[str] = []


# --- credit notes --------------------------------------------------------


class CreditNoteUsageOut(ORMModel):
    id: UUID
    sale_id: UUID
    invoice_no: str | None = None
    amount_used: Decimal
    used_at: str


class CreditNoteOut(ORMModel):
    id: UUID
    credit_note_no: str
    customer_id: UUID
    customer_mobile: str | None = None
    customer_name: str | None = None
    sale_return_id: UUID | None = None
    issue_date: date
    amount_issued: Decimal
    amount_used: Decimal
    balance_amount: Decimal
    expires_on: date
    days_to_expiry: int
    status: CreditNoteStatus
    notes: str | None = None
    usages: list[CreditNoteUsageOut] = []


class CreditNoteCancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
