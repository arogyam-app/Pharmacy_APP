"""Inventory schemas.

Cost price must never reach a non-admin, so it is absent from the *type* a
sales executive receives rather than merely hidden in the UI
(CLAUDE.md rule 4).
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from app.models.enums import ExpiryBasis, ItemType, PackUnit
from app.schemas.common import ORMModel


class ExpiryIn(BaseModel):
    """The three ways a pack states its shelf life.

    Omit `expiry_basis` and the server infers it from what is filled in, so a
    client that only knows about expiry dates keeps working untouched.
    """

    #: EXPIRY_DATE (printed on the pack), BEST_BEFORE (computed from the two
    #: fields below) or NONE (a comb does not go off).
    expiry_basis: ExpiryBasis | None = None
    expiry_date: date | None = None
    mfg_date: date | None = None
    best_before_months: int | None = Field(default=None, ge=1, le=600)


class BatchBase(ExpiryIn):
    # Required for a medicine, optional for a general item -- enforced in
    # services.inventory.resolve_batch_no, which is where the medicine's
    # item_type is known. A blank one there is not rejected here; it is
    # replaced with a generated placeholder.
    batch_no: str | None = Field(default=None, max_length=60)
    mrp_per_pack: Decimal = Field(gt=0, le=9999999)
    gst_rate: Decimal = Field(default=Decimal("12.00"), ge=0, le=100)
    default_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _discount_order(self) -> "BatchBase":
        if (
            self.default_discount_pct is not None
            and self.max_discount_pct is not None
            and self.default_discount_pct > self.max_discount_pct
        ):
            raise ValueError("Default discount cannot exceed the maximum discount.")
        return self


class BatchCreate(BatchBase):
    medicine_id: UUID
    # Quantity received, in packs (strips/bottles) — converted to units by
    # the server using the medicine's units_per_pack.
    qty_packs: Decimal = Field(gt=0, le=1000000)
    cost_per_pack: Decimal | None = Field(default=None, ge=0, le=9999999)
    received_date: date | None = None


class BatchUpdate(BaseModel):
    """Prices and limits can be corrected; quantity only moves via the ledger."""

    mrp_per_pack: Decimal | None = Field(default=None, gt=0, le=9999999)
    cost_per_pack: Decimal | None = Field(default=None, ge=0, le=9999999)
    gst_rate: Decimal | None = Field(default=None, ge=0, le=100)
    default_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    # Correcting the shelf life means restating how it was arrived at, so
    # these travel together and the server re-resolves them (see
    # services.inventory.resolve_expiry).
    expiry_basis: ExpiryBasis | None = None
    expiry_date: date | None = None
    mfg_date: date | None = None
    best_before_months: int | None = Field(default=None, ge=1, le=600)
    is_blocked: bool | None = None
    block_reason: str | None = Field(default=None, max_length=200)


class StockAdjustment(BaseModel):
    """A signed correction. Positive adds units, negative removes them."""

    qty_units_delta: Decimal = Field(ge=-1000000, le=1000000)
    # Required: an unexplained stock change is indistinguishable from theft.
    reason: str = Field(min_length=3, max_length=200)

    @model_validator(mode="after")
    def _not_zero(self) -> "StockAdjustment":
        if self.qty_units_delta == 0:
            raise ValueError("An adjustment cannot be zero.")
        return self


class BatchPublic(ORMModel):
    """What a sales executive may see — no cost, no margin."""

    id: UUID
    medicine_id: UUID
    medicine_name: str | None = None
    batch_no: str
    #: The resolved shelf date. Meaningless when `has_expiry` is false —
    #: it holds the never-expires sentinel — so read that first.
    expiry_date: date
    expiry_basis: ExpiryBasis = ExpiryBasis.EXPIRY_DATE
    has_expiry: bool = True
    mfg_date: date | None = None
    best_before_months: int | None = None
    pack_unit: PackUnit
    pack_size: Decimal
    units_per_pack: int
    qty_received_units: Decimal
    qty_available_units: Decimal
    qty_available_packs: Decimal
    mrp_per_pack: Decimal
    unit_mrp: Decimal
    gst_rate: Decimal
    default_discount_pct: Decimal | None = None
    max_discount_pct: Decimal | None = None
    effective_default_discount_pct: Decimal
    effective_max_discount_pct: Decimal
    received_date: date | None = None
    is_blocked: bool
    block_reason: str | None = None
    is_expired: bool
    #: Null for stock that never expires — there is no countdown to show.
    days_to_expiry: int | None = None


class BatchAdmin(BatchPublic):
    """Adds the figures only an owner should see."""

    cost_per_pack: Decimal | None = None
    unit_cost: Decimal | None = None
    margin_pct: Decimal | None = None


class StockLedgerEntry(ORMModel):
    id: UUID
    batch_id: UUID
    medicine_id: UUID
    movement_type: str
    qty_units_delta: Decimal
    balance_after_units: Decimal
    reference_type: str | None = None
    reference_id: UUID | None = None
    reason: str | None = None
    created_at: str | None = None


class MedicineStockRow(BaseModel):
    """One row of the stock-on-hand screen."""

    medicine_id: UUID
    brand_name: str
    #: Null for a general item — soap has no formulation.
    composition_text: str | None = None
    manufacturer_name: str
    item_type: ItemType = ItemType.MEDICINE
    pack_label: str
    pack_unit: PackUnit
    units_per_pack: int
    is_countable: bool
    available_units: Decimal
    available_packs: Decimal
    batch_count: int
    nearest_expiry: date | None = None
    has_near_expiry: bool
    mrp_per_pack: Decimal | None = None
    unit_mrp: Decimal | None = None
    gst_rate: Decimal | None = None
    reorder_level_units: Decimal | None = None
    is_low_stock: bool
