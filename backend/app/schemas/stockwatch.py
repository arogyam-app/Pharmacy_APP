"""Alerts, expiry register and order-book schemas.

Cost figures appear only in the admin-facing models, the same rule as
everywhere else (CLAUDE.md rule 4).
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.enums import ExpirySource, ExpiryStatus, OrderBookStatus, OrderBookTrigger
from app.schemas.common import ORMModel


# --- alerts --------------------------------------------------------------


class AlertSummary(BaseModel):
    """The counts behind the nav badges."""

    expired: int
    near_expiry: int
    low_stock: int
    out_of_stock: int
    order_book_open: int
    near_expiry_days: int
    # What the expired stock is worth at retail — the figure that makes the
    # case for acting on it.
    expired_value: Decimal


class BatchAlertRow(BaseModel):
    batch_id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    batch_no: str
    expiry_date: date
    days_to_expiry: int
    qty_units: Decimal
    qty_packs: Decimal
    mrp_value: Decimal
    is_blocked: bool


class BatchAlertRowAdmin(BatchAlertRow):
    cost_value: Decimal | None = None


class StockAlertRow(BaseModel):
    medicine_id: UUID
    brand_name: str
    pack_label: str
    units_per_pack: int
    available_units: Decimal
    available_packs: Decimal
    reorder_level_units: Decimal
    rack_location: str | None = None


# --- expiry register -----------------------------------------------------


class ExpiryScanResult(BaseModel):
    added: int
    updated: int
    scanned: int
    message: str


class ExpiryEntryOut(ORMModel):
    id: UUID
    medicine_id: UUID
    medicine_name: str
    batch_id: UUID
    batch_no: str
    expiry_date: date
    days_expired: int
    qty_units: Decimal
    mrp_value: Decimal
    source: ExpirySource
    status: ExpiryStatus
    action_date: date | None = None
    actioned_by_name: str | None = None
    supplier_id: UUID | None = None
    supplier_name: str | None = None
    debit_note_ref: str | None = None
    credit_received_amount: Decimal | None = None
    notes: str | None = None


class ExpiryEntryAdmin(ExpiryEntryOut):
    cost_value: Decimal | None = None


class WriteOffIn(BaseModel):
    notes: str | None = Field(default=None, max_length=500)


class ReturnToSupplierIn(BaseModel):
    supplier_id: UUID
    # Required: goods leaving against an expected credit need a reference,
    # or there is nothing to chase the distributor with.
    debit_note_ref: str = Field(min_length=1, max_length=60)
    credit_expected: Decimal | None = Field(default=None, ge=0, le=9999999)
    notes: str | None = Field(default=None, max_length=500)


class RecordCreditIn(BaseModel):
    amount: Decimal = Field(ge=0, le=9999999)


# --- order book ----------------------------------------------------------


class OrderBookRow(ORMModel):
    id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    pack_label: str | None = None
    units_per_pack: int | None = None
    trigger_reason: OrderBookTrigger
    baseline_units: Decimal
    available_units_at_trigger: Decimal
    available_units_now: Decimal | None = None
    decline_pct: Decimal
    suggested_qty_units: Decimal
    suggested_qty_packs: Decimal
    avg_monthly_consumption_units: Decimal
    days_of_cover: int | None = None
    preferred_supplier_id: UUID | None = None
    preferred_supplier_name: str | None = None
    status: OrderBookStatus
    ordered_at: str | None = None
    purchase_invoice_id: UUID | None = None
    closed_at: str | None = None
    notes: str | None = None


class OrderBookRowAdmin(OrderBookRow):
    last_purchase_cost: Decimal | None = None
    estimated_value: Decimal | None = None


class OrderBookUpdate(BaseModel):
    suggested_qty_packs: Decimal | None = Field(default=None, gt=0, le=100000)
    preferred_supplier_id: UUID | None = None
    status: OrderBookStatus | None = None
    notes: str | None = Field(default=None, max_length=500)


class RecomputeResult(BaseModel):
    raised: int
    refreshed: int
    closed: int
    message: str
