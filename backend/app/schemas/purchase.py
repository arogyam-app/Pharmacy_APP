"""Supplier and goods-inward schemas.

Every purchase route is admin-only, so there is no Public/Admin split here —
cost price is the whole point of the document. The gating that matters is on
the route (CLAUDE.md rule 4), and on `batches`, where the cost these lines
create is served through a different response model.
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.enums import ExpiryBasis, PurchasePaymentStatus
from app.schemas.catalog import FormHintOut, MedicineMatch, PackHintOut
from app.schemas.common import ORMModel

# ₹ figures a pharmacy will realistically type; wide enough for a full
# consignment, narrow enough that a slipped decimal point is caught.
MAX_AMOUNT = Decimal("9999999")


# --- suppliers -----------------------------------------------------------


class SupplierBase(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    gstin: str | None = Field(default=None, min_length=15, max_length=15)
    # Drives CGST+SGST vs IGST on every invoice from this supplier.
    state_code: str | None = Field(default=None, min_length=1, max_length=2)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=15)
    email: EmailStr | None = None
    contact_person: str | None = Field(default=None, max_length=120)
    drug_licence_no: str | None = Field(default=None, max_length=50)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)


class SupplierCreate(SupplierBase):
    pass


class SupplierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    gstin: str | None = Field(default=None, min_length=15, max_length=15)
    state_code: str | None = Field(default=None, min_length=1, max_length=2)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=15)
    email: EmailStr | None = None
    contact_person: str | None = Field(default=None, max_length=120)
    drug_licence_no: str | None = Field(default=None, max_length=50)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    is_active: bool | None = None


class SupplierOut(ORMModel):
    id: UUID
    name: str
    gstin: str | None = None
    state_code: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    contact_person: str | None = None
    drug_licence_no: str | None = None
    payment_terms_days: int | None = None
    is_active: bool
    purchase_count: int | None = None


# --- purchase lines ------------------------------------------------------


class PurchaseItemIn(BaseModel):
    """One line of the supplier's invoice.

    Quantities are in packs — strips, bottles, vials — because that is how a
    distributor bills. The server converts to sellable units using the
    medicine's own `units_per_pack`.
    """

    medicine_id: UUID
    # Required for a medicine, optional for a general item -- enforced in
    # services.inventory.resolve_batch_no, once the medicine (and so its
    # item_type) is known.
    batch_no: str | None = Field(default=None, max_length=60)
    # Whatever the carton states: a printed expiry, a manufacturing date with
    # a best-before period, or nothing at all for goods that never expire.
    # The server resolves the three into one shelf date — see
    # services.inventory.resolve_expiry.
    expiry_basis: ExpiryBasis | None = None
    expiry_date: date | None = None
    mfg_date: date | None = None
    best_before_months: int | None = Field(default=None, ge=1, le=600)
    qty_packs: Decimal = Field(gt=0, le=1000000)
    # Scheme goods: received and sellable, but not billed.
    free_qty_packs: Decimal = Field(default=Decimal("0"), ge=0, le=1000000)
    # GST-exclusive rate, as printed on the distributor's invoice.
    cost_per_pack: Decimal = Field(ge=0, le=MAX_AMOUNT)
    mrp_per_pack: Decimal = Field(gt=0, le=MAX_AMOUNT)
    discount_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    gst_rate: Decimal = Field(default=Decimal("12.00"), ge=0, le=100)
    # Optional retail policy for the batch this line creates; NULL falls
    # back to the store settings.
    default_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _checks(self) -> "PurchaseItemIn":
        if (
            self.default_discount_pct is not None
            and self.max_discount_pct is not None
            and self.default_discount_pct > self.max_discount_pct
        ):
            raise ValueError("Default discount cannot exceed the maximum discount.")
        if self.cost_per_pack > self.mrp_per_pack:
            raise ValueError("Cost cannot exceed MRP — check the rate and the MRP.")
        return self


class PurchaseCreate(BaseModel):
    supplier_id: UUID
    supplier_invoice_no: str = Field(min_length=1, max_length=50)
    supplier_invoice_date: date
    received_date: date | None = None
    # NULL lets the server decide from the two state codes.
    is_intra_state: bool | None = None
    other_charges: Decimal = Field(default=Decimal("0"), ge=0, le=MAX_AMOUNT)
    payment_status: PurchasePaymentStatus = PurchasePaymentStatus.UNPAID
    notes: str | None = Field(default=None, max_length=1000)
    items: list[PurchaseItemIn] = Field(min_length=1)


class PurchaseUpdate(BaseModel):
    """Only the commercial wrapper is editable.

    Lines, quantities and tax are the supplier's document and this shop's
    stock; correcting those means cancelling and re-entering.
    """

    payment_status: PurchasePaymentStatus | None = None
    notes: str | None = Field(default=None, max_length=1000)


class PurchaseCancel(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


class PurchaseItemOut(ORMModel):
    id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    batch_id: UUID | None = None
    batch_no: str
    expiry_date: date
    units_per_pack: int
    qty_packs: Decimal
    free_qty_packs: Decimal
    qty_units: Decimal
    cost_per_pack: Decimal
    mrp_per_pack: Decimal
    discount_pct: Decimal
    gst_rate: Decimal
    taxable_value: Decimal
    discount_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    line_total: Decimal


class PurchaseRow(ORMModel):
    """One line of the purchase register."""

    id: UUID
    doc_number: str
    supplier_id: UUID
    supplier_name: str | None = None
    supplier_invoice_no: str
    supplier_invoice_date: date
    received_date: date
    item_count: int = 0
    #: Which tax the document carries. On the register so a client adding
    #: lines to it can preview the split correctly instead of assuming the
    #: store's own state.
    is_intra_state: bool = True
    subtotal_taxable: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    igst_amount: Decimal
    total_amount: Decimal
    payment_status: str
    is_cancelled: bool


class PurchaseOut(PurchaseRow):
    discount_amount: Decimal
    other_charges: Decimal
    round_off: Decimal
    notes: str | None = None
    cancel_reason: str | None = None
    cancelled_at: str | None = None
    items: list[PurchaseItemOut] = []
    # Non-fatal observations from posting — a differing MRP on a repeat
    # batch, an already-expired lot. Present only on the create response.
    warnings: list[str] = []


# --- what a non-admin sees ------------------------------------------------
#
# The Purchases module can be handed to a trusted executive, who then does
# goods inward: booking in what the distributor delivered. That job is about
# quantities, batches and expiry dates. Every figure below is deliberately
# absent — cost per pack, the taxable value and tax derived from it, the
# invoice total — because each one states what the shop paid, and cost prices
# do not leave the API for a non-admin (CLAUDE.md rule 4).
#
# MRP stays: it is printed on the box and the counter sells at it.


class PurchaseItemsAdd(BaseModel):
    """Lines added to an invoice already received.

    Only additions: the document keeps its number, its supplier and every
    line already on it. Correcting or removing a line is still
    cancel-and-re-enter, because that is the case where the stock may
    already have been sold.
    """

    items: list[PurchaseItemIn] = Field(min_length=1, max_length=100)


class ImportedLine(BaseModel):
    """One row of an uploaded item list, parsed and matched but **not** applied.

    Everything the manual form would have collected, plus the candidates the
    row's product text might mean. `medicine_id` is deliberately absent:
    resolving the text to a product is the operator's decision, and the
    resolved lines come back through the ordinary goods-inward call.
    """

    row: int
    product_text: str
    batch_no: str = ""
    expiry_basis: ExpiryBasis = ExpiryBasis.EXPIRY_DATE
    expiry_date: date | None = None
    mfg_date: date | None = None
    best_before_months: int | None = None
    qty_packs: Decimal | None = None
    free_qty_packs: Decimal = Decimal("0")
    mrp_per_pack: Decimal | None = None
    cost_per_pack: Decimal | None = None
    discount_pct: Decimal = Decimal("0")
    gst_rate: Decimal = Decimal("12")
    default_discount_pct: Decimal | None = None
    max_discount_pct: Decimal | None = None
    #: What the product text might mean, best first. Empty when nothing on
    #: the master resembles it.
    matches: list["MedicineMatch"] = []
    #: A pack size read off the product text, when it states one.
    pack_hint: "PackHintOut | None" = None
    #: False with a `pack_hint` set means the brand is on the master but that
    #: size is not — two sizes are two products, so it has to be added.
    has_pack_match: bool = False
    #: A dosage form read off the product text (TAB/CAP/SYP…), when it names one.
    form_hint: "FormHintOut | None" = None
    #: False with a `form_hint` set means the brand is on the master but not
    #: in that form — a tablet is not what "PARACETAMOL SYP" meant.
    has_form_match: bool = False
    #: Why this row cannot be received as it stands.
    errors: list[str] = []


class PurchaseImportPreview(BaseModel):
    """An uploaded item list, ready for a person to resolve.

    Nothing has been received. `POST /purchases` (or `/purchases/{id}/items`)
    still does that, once every line names a real product.
    """

    filename: str | None = None
    total_rows: int
    #: Rows with no problems *and* exactly one candidate that is the right
    #: pack — still shown, still confirmed, never auto-accepted.
    ready_rows: int
    lines: list[ImportedLine] = []
    #: Header columns the file was missing, if any.
    missing_columns: list[str] = []


class PurchaseItemPublic(ORMModel):
    id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    batch_id: UUID | None = None
    batch_no: str
    expiry_date: date
    units_per_pack: int
    qty_packs: Decimal
    free_qty_packs: Decimal
    qty_units: Decimal
    mrp_per_pack: Decimal
    gst_rate: Decimal


class PurchaseRowPublic(ORMModel):
    """One line of the purchase register, without the money."""

    id: UUID
    doc_number: str
    supplier_id: UUID
    supplier_name: str | None = None
    supplier_invoice_no: str
    supplier_invoice_date: date
    received_date: date
    item_count: int = 0
    payment_status: str
    is_cancelled: bool


class PurchaseOutPublic(PurchaseRowPublic):
    is_intra_state: bool
    notes: str | None = None
    cancel_reason: str | None = None
    cancelled_at: str | None = None
    items: list[PurchaseItemPublic] = []
    warnings: list[str] = []


def redact(model, source) -> object:
    """Rebuild `source` as the cost-free `model`, dropping what it omits.

    Written as a projection rather than a second construction path so the two
    shapes cannot drift: a money field added to the admin model is absent
    from the public one until somebody deliberately lists it.
    """
    data = {
        key: value for key, value in source.model_dump().items() if key in model.model_fields
    }
    if "items" in model.model_fields:
        data["items"] = [
            redact(PurchaseItemPublic, item) for item in getattr(source, "items", [])
        ]
    return model(**data)
