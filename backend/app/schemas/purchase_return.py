"""Debit notes: goods back to a distributor, and the credit that creates.

Cost price is the whole substance of a return document, so unlike most
resources here there is no meaningful "public" shape of a *line*. What the
non-admin split protects is the money: `…Public` reports the quantities and
the reference numbers, and drops every figure derived from cost (CLAUDE.md
rule 4).
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PurchaseReturnStatus, SupplierCreditStatus
from app.schemas.common import ORMModel


class PurchaseReturnLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    qty_units: Decimal = Field(gt=0, description="In the smallest sellable unit, not packs")
    #: Defaults to the batch's recorded cost. Given explicitly only when the
    #: distributor has agreed to credit something else.
    cost_per_unit: Decimal | None = Field(default=None, ge=0)


class PurchaseReturnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID
    #: Optional — stock bought across several invoices months ago still gets
    #: returned, and forcing a link would mean inventing one.
    purchase_invoice_id: UUID | None = None
    reason: str = Field(min_length=3, max_length=500)
    supplier_credit_note_ref: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=500)
    items: list[PurchaseReturnLineIn] = Field(min_length=1)


class PurchaseReturnCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class CreditApply(BaseModel):
    """Set part of a debit note against a purchase invoice."""

    model_config = ConfigDict(extra="forbid")

    purchase_return_id: UUID
    amount: Decimal = Field(gt=0)


class PurchaseReturnItemPublic(ORMModel):
    id: UUID
    line_no: int
    medicine_id: UUID
    medicine_name: str
    pack_label: str | None = None
    hsn_code: str | None = None
    batch_id: UUID
    batch_no: str
    expiry_date: date
    #: False for goods that never expire — `expiry_date` is a sentinel then
    #: and must not be printed.
    has_expiry: bool = True
    qty_units: Decimal


class PurchaseReturnItemAdmin(PurchaseReturnItemPublic):
    cost_per_unit: Decimal
    gst_rate: Decimal
    taxable_value: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total: Decimal


class PurchaseReturnRowPublic(ORMModel):
    id: UUID
    debit_note_no: str
    supplier_id: UUID
    supplier_name: str | None = None
    return_date: date
    reason: str
    supplier_credit_note_ref: str | None = None
    status: PurchaseReturnStatus
    item_count: int = 0


class PurchaseReturnRow(PurchaseReturnRowPublic):
    is_intra_state: bool
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_amount: Decimal
    credit_used: Decimal
    credit_balance: Decimal
    credit_status: SupplierCreditStatus


class PurchaseReturnOutPublic(PurchaseReturnRowPublic):
    purchase_invoice_id: UUID | None = None
    notes: str | None = None
    cancel_reason: str | None = None
    items: list[PurchaseReturnItemPublic] = []


class PurchaseReturnOut(PurchaseReturnRow):
    purchase_invoice_id: UUID | None = None
    notes: str | None = None
    cancel_reason: str | None = None
    items: list[PurchaseReturnItemAdmin] = []


class SupplierCreditRow(ORMModel):
    """One debit note, seen as money still owed by the distributor."""

    purchase_return_id: UUID
    debit_note_no: str
    return_date: date
    total_amount: Decimal
    credit_used: Decimal
    credit_balance: Decimal
    credit_status: SupplierCreditStatus


class CreditUsageOut(ORMModel):
    id: UUID
    purchase_return_id: UUID
    debit_note_no: str | None = None
    purchase_invoice_id: UUID
    amount_used: Decimal
    used_at: str
