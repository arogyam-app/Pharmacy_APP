"""POS and invoice schemas.

The client sends *what* to sell, never *what it costs*: a medicine, a
quantity in sellable units, optionally a batch and a requested discount.
Every price on the way back is the server's (CLAUDE.md rule 8).

Unit cost and margin exist on a sale line but appear only in the `…Admin`
response models, so a sales executive's token cannot surface them whatever
the client asks for (rule 4).
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import ItemType, PaymentMode, SaleStatus
from app.schemas.common import ORMModel

MAX_AMOUNT = Decimal("9999999")


# --- what the counter sends ----------------------------------------------


class CartLine(BaseModel):
    """One thing the customer is buying.

    `qty_units` is in **sellable units** — tablets for a strip-based pack,
    whole bottles for a syrup — because that is what stock is counted in and
    what makes a single-tablet sale possible.
    """

    medicine_id: UUID
    qty_units: Decimal = Field(gt=0, le=100000)
    # Omit to let the server pick by FEFO; set it to honour a customer who
    # wants the same lot as last time.
    batch_id: UUID | None = None
    # Omit to accept the suggested discount for that batch.
    discount_pct: Decimal | None = Field(default=None, ge=0, le=100)

    @field_validator("qty_units")
    @classmethod
    def _whole_units(cls, value: Decimal) -> Decimal:
        """Units are indivisible: half a tablet or a third of a bottle is
        not a thing the counter can hand over."""
        if value != value.to_integral_value():
            raise ValueError("Quantity must be a whole number of units.")
        return value


class CustomerRef(BaseModel):
    """Either an existing customer, or a mobile to look up or quick-create."""

    customer_id: UUID | None = None
    mobile: str | None = Field(default=None, max_length=15)
    name: str | None = Field(default=None, max_length=150)
    gstin: str | None = Field(default=None, min_length=15, max_length=15)
    state_code: str | None = Field(default=None, min_length=1, max_length=2)


class QuoteRequest(BaseModel):
    items: list[CartLine] = Field(min_length=1)
    customer: CustomerRef | None = None
    doctor_id: UUID | None = None
    discount_override_reason: str | None = Field(default=None, max_length=200)


class PaymentIn(BaseModel):
    mode: PaymentMode
    amount: Decimal = Field(gt=0, le=MAX_AMOUNT)
    reference_no: str | None = Field(default=None, max_length=60)
    credit_note_id: UUID | None = None


class SaleCreate(BaseModel):
    items: list[CartLine] = Field(min_length=1)
    customer: CustomerRef | None = None
    doctor_id: UUID | None = None
    prescription_ref: str | None = Field(default=None, max_length=100)
    payments: list[PaymentIn] = Field(min_length=1)
    # A retried submit with the same key returns the first invoice instead
    # of billing the customer twice.
    idempotency_key: str | None = Field(default=None, max_length=64)
    discount_override_reason: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _override_needs_admin_reason(self) -> "SaleCreate":
        if self.discount_override_reason is not None and len(
            self.discount_override_reason.strip()
        ) < 3:
            raise ValueError("A discount override needs a real reason.")
        return self


# --- what comes back ------------------------------------------------------


class QuotedLineOut(BaseModel):
    """One priced line. A cart line spanning two batches returns twice."""

    line_no: int
    medicine_id: UUID
    medicine_name: str
    pack_label: str | None = None
    composition_text: str | None = None
    batch_id: UUID
    batch_no: str
    expiry_date: date
    #: False for goods that never expire — `expiry_date` is then a sentinel
    #: and must not be printed.
    has_expiry: bool = True
    units_per_pack: int
    qty_units: Decimal
    qty_packs: Decimal
    unit_mrp: Decimal
    mrp_per_pack: Decimal
    gross_amount: Decimal
    discount_pct: Decimal
    max_discount_pct: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    gst_rate: Decimal
    taxable_value: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    is_near_expiry: bool
    #: Null for goods that never expire.
    days_to_expiry: int | None = None


class TotalsOut(BaseModel):
    gross_amount: Decimal
    discount_amount: Decimal
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    gst_amount: Decimal
    sub_total: Decimal
    round_off: Decimal
    net_amount: Decimal


class QuoteOut(BaseModel):
    lines: list[QuotedLineOut]
    totals: TotalsOut
    is_intra_state: bool
    requires_doctor: bool
    # Near expiry, multi-batch splits — worth telling the operator, none of
    # them fatal.
    warnings: list[str] = []
    amount_in_words: str


class SaleItemOut(ORMModel):
    id: UUID
    line_no: int
    medicine_id: UUID
    medicine_name: str
    item_type: ItemType = ItemType.MEDICINE
    pack_label: str | None = None
    hsn_code: str | None = None
    batch_id: UUID
    batch_no: str
    expiry_date: date
    #: False for goods that never expire — `expiry_date` is then a sentinel
    #: and must not be printed.
    has_expiry: bool = True
    units_per_pack: int
    qty_units: Decimal
    qty_packs: Decimal
    unit_mrp: Decimal
    mrp_per_pack: Decimal
    gross_amount: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    gst_rate: Decimal
    taxable_value: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    returned_qty_units: Decimal


class SaleItemAdmin(SaleItemOut):
    """Adds the figures only an owner should see."""

    unit_cost: Decimal | None = None
    line_cost: Decimal | None = None
    margin_amount: Decimal | None = None
    margin_pct: Decimal | None = None


class PaymentOut(ORMModel):
    id: UUID
    mode: str
    amount: Decimal
    reference_no: str | None = None
    credit_note_id: UUID | None = None
    # A bill settled from a credit note has to name it, and say what is left
    # on account — otherwise the customer has no record of either.
    credit_note_no: str | None = None
    credit_note_balance: Decimal | None = None


class SaleRow(BaseModel):
    """One line of the sales register."""

    id: UUID
    invoice_no: str
    invoice_date: date
    invoice_datetime: str
    customer_mobile: str | None = None
    customer_name: str | None = None
    doctor_name: str | None = None
    sales_user_name: str | None = None
    item_count: int = 0
    gross_amount: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    status: SaleStatus
    reprint_count: int


class SaleOut(SaleRow):
    customer_id: UUID | None = None
    customer_gstin: str | None = None
    doctor_id: UUID | None = None
    prescription_ref: str | None = None
    is_intra_state: bool
    taxable_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    round_off: Decimal
    paid_amount: Decimal
    discount_override_reason: str | None = None
    notes: str | None = None
    amount_in_words: str
    items: list[SaleItemOut | SaleItemAdmin] = []
    payments: list[PaymentOut] = []
    warnings: list[str] = []


class TaxSummaryRow(BaseModel):
    """One GST rate's worth of an invoice — what the printed bill shows."""

    gst_rate: Decimal
    taxable_value: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_tax: Decimal


class StoreHeader(BaseModel):
    name: str
    legal_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    gstin: str | None = None
    drug_licence_no_20b: str | None = None
    drug_licence_no_21b: str | None = None
    phone: str | None = None
    email: str | None = None
    # Letterhead branding — data URIs or URLs, printed by the A4 layout.
    logo_image: str | None = None
    partner_logo_image: str | None = None
    order_qr_image: str | None = None
    order_qr_caption: str | None = None


class InvoicePrint(BaseModel):
    """Everything the SPA needs to render a bill — the server sends no HTML."""

    store: StoreHeader
    sale: SaleOut
    tax_summary: list[TaxSummaryRow]
    #: The invoice value, in words. Unchanged by a credit note — the note is
    #: a payment, not a discount, and the tax invoice still stands for its
    #: full amount.
    amount_in_words: str
    #: Credit notes spent on this bill, and what is therefore left to hand
    #: over. Equal to `sale.net_amount` when no note was used.
    credit_note_applied: Decimal = Decimal("0.00")
    amount_payable: Decimal
    amount_payable_in_words: str
    invoice_prefix: str | None = None
    footer_text: str | None = None
    print_format: str
    gst_enabled: bool
    #: Whether this bill has been printed before. Rendering the payload does
    #: not make it true — only `POST /sales/{id}/printed` does.
    is_reprint: bool
    reprint_count: int
    printed_at: str


class PrintRecord(BaseModel):
    """The acknowledgement of one print, recorded when paper actually moves.

    Opening an invoice on screen is a read and counts nothing; the client
    posts here when the operator commits to printing, so the number in the
    register is a count of sheets rather than a count of glances.
    """

    invoice_no: str
    #: Prints so far, including this one.
    reprint_count: int
    #: True when this print is a duplicate — that is, not the first.
    is_reprint: bool
    printed_at: str
