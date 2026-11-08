"""Catalogue schemas: salts, compositions, manufacturers, medicines.

The medicine master holds identity only — brand, maker, composition, form,
pack, schedule, HSN. Money (MRP, GST rate, discount limits) lives on the
batch, so no cost data passes through here and there is no Public/Admin
split; that split is in app/schemas/inventory.py.

Price and stock still *appear* on medicine responses, but as read-only
figures resolved from the batch that would be sold next.
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.enums import (
    PACK_UNITS_BY_FORM,
    DosageForm,
    ItemType,
    PackUnit,
    ScheduleType,
)
from app.schemas.common import ORMModel


# --- salts ---------------------------------------------------------------


class SaltOut(ORMModel):
    id: UUID
    name: str
    therapeutic_class: str | None = None
    is_active: bool


class SaltCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    therapeutic_class: str | None = Field(default=None, max_length=150)


class SaltUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    therapeutic_class: str | None = Field(default=None, max_length=150)
    is_active: bool | None = None


# --- compositions --------------------------------------------------------


class ComponentIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    strength: str | None = Field(default=None, max_length=50)


class ComponentOut(BaseModel):
    salt_id: UUID
    name: str
    strength: str | None = None


class CompositionOut(ORMModel):
    id: UUID
    composition_text: str
    therapeutic_class: str | None = None
    notes: str | None = None
    is_active: bool
    components: list[ComponentOut] = []
    medicine_count: int | None = None


class CompositionCreate(BaseModel):
    """Give either a free-text formulation or its parsed components; both are
    canonicalised to the same row."""

    composition_text: str | None = Field(default=None, max_length=1000)
    components: list[ComponentIn] | None = None
    therapeutic_class: str | None = Field(default=None, max_length=150)
    notes: str | None = None

    @model_validator(mode="after")
    def _need_one(self) -> "CompositionCreate":
        if not self.composition_text and not self.components:
            raise ValueError("Provide composition_text or components.")
        return self


class SimilarCompositions(BaseModel):
    """Live duplicate check while a formulation is being typed."""

    # Non-null when the text canonicalises to a composition that already
    # exists — use that one instead of creating a second.
    exact: CompositionOut | None = None
    similar: list[CompositionOut] = []


class CompositionUpdate(BaseModel):
    """Supplying a formulation re-groups substitutes.

    Every medicine on this composition is an alternative of every other, so
    changing the salts changes what the counter will offer. The server
    recanonicalises and refuses a change that would collide with a
    composition that already exists.
    """

    composition_text: str | None = Field(default=None, max_length=1000)
    components: list[ComponentIn] | None = None
    therapeutic_class: str | None = Field(default=None, max_length=150)
    notes: str | None = None
    is_active: bool | None = None


class DeleteResult(BaseModel):
    deleted: bool
    id: UUID
    message: str


# --- manufacturers -------------------------------------------------------


class ManufacturerOut(ORMModel):
    id: UUID
    name: str
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool


class ManufacturerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=15)
    email: EmailStr | None = None


class ManufacturerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=15)
    email: EmailStr | None = None
    is_active: bool | None = None


# --- medicines -----------------------------------------------------------


class MedicineBase(BaseModel):
    brand_name: str = Field(min_length=1, max_length=200)
    #: MEDICINE or GENERAL. A general item carries no composition, no
    #: schedule and no dosage form; the server forces those to their neutral
    #: values rather than trusting the client to leave them alone.
    item_type: ItemType = ItemType.MEDICINE
    dosage_form: DosageForm = DosageForm.TABLET
    # Pack measure: 10 TABLET, 100 ML, 20 GM… `units_per_pack` is derived
    # from these by the server, never sent by the client.
    pack_size: Decimal = Field(default=Decimal("1"), gt=0, le=100000)
    pack_unit: PackUnit | None = None
    pack_label: str = Field(default="", max_length=60)
    schedule_type: ScheduleType = ScheduleType.OTC
    is_prescription_required: bool = False
    # HSN is a stable product classification and stays on the master. MRP,
    # GST rate and discount limits belong to the batch — they change with
    # every consignment (see app/schemas/inventory.py).
    hsn_code: str | None = Field(default=None, max_length=10)
    # The manufacturer's list price, for reference only — a sale is always
    # billed from the MRP on the batch being sold.
    list_mrp_per_pack: Decimal | None = Field(default=None, ge=0, le=1000000)
    reorder_level_units: Decimal | None = Field(default=None, ge=0)
    rack_location: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _general_items_are_not_medicines(self) -> "MedicineBase":
        """Keep the pharmacological fields meaningless for a general item.

        Rather than reject a client that sends a schedule for a bar of soap,
        normalise it: GENERAL always means dosage form GENERAL, over the
        counter, no prescription. There is nothing to check a prescription
        against.
        """
        if self.item_type is ItemType.GENERAL:
            self.dosage_form = DosageForm.GENERAL
            self.schedule_type = ScheduleType.OTC
            self.is_prescription_required = False
        elif self.dosage_form is DosageForm.GENERAL:
            raise ValueError(
                "GENERAL is not a dosage form. Set item_type to GENERAL instead."
            )
        return self

    @model_validator(mode="after")
    def _pack_unit_suits_the_form(self) -> "MedicineBase":
        if self.pack_unit is None:
            return self
        allowed = PACK_UNITS_BY_FORM.get(self.dosage_form, [PackUnit.UNIT])
        if self.pack_unit not in allowed:
            allowed_text = ", ".join(u.value for u in allowed)
            raise ValueError(
                f"A {self.dosage_form.value.lower()} is measured in {allowed_text}, "
                f"not {self.pack_unit.value}."
            )
        return self


class MedicineCreate(MedicineBase):
    manufacturer_id: UUID | None = None
    manufacturer_name: str | None = Field(default=None, max_length=200)
    composition_id: UUID | None = None
    composition_text: str | None = Field(default=None, max_length=1000)
    therapeutic_class: str | None = Field(default=None, max_length=150)

    @model_validator(mode="after")
    def _need_identity(self) -> "MedicineCreate":
        if self.manufacturer_id is None and not self.manufacturer_name:
            raise ValueError("Provide manufacturer_id or manufacturer_name.")
        if self.item_type is ItemType.GENERAL:
            # Soap has no salt. Silently drop anything sent, so a form that
            # keeps its composition box around cannot attach one.
            self.composition_id = None
            self.composition_text = None
        elif self.composition_id is None and not self.composition_text:
            raise ValueError("Provide composition_id or composition_text.")
        return self


class MedicineUpdate(BaseModel):
    brand_name: str | None = Field(default=None, min_length=1, max_length=200)
    manufacturer_id: UUID | None = None
    composition_id: UUID | None = None
    composition_text: str | None = Field(default=None, max_length=1000)
    dosage_form: DosageForm | None = None
    pack_size: Decimal | None = Field(default=None, gt=0, le=100000)
    pack_unit: PackUnit | None = None
    pack_label: str | None = Field(default=None, max_length=60)
    schedule_type: ScheduleType | None = None
    is_prescription_required: bool | None = None
    hsn_code: str | None = Field(default=None, max_length=10)
    list_mrp_per_pack: Decimal | None = Field(default=None, ge=0, le=1000000)
    reorder_level_units: Decimal | None = Field(default=None, ge=0)
    rack_location: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None


class MedicineOut(ORMModel):
    id: UUID
    store_id: UUID
    brand_name: str
    manufacturer_id: UUID
    manufacturer_name: str
    item_type: ItemType = ItemType.MEDICINE
    #: Null for a general item.
    composition_id: UUID | None = None
    composition_text: str | None = None
    therapeutic_class: str | None = None
    dosage_form: DosageForm
    pack_size: Decimal
    pack_unit: PackUnit
    pack_label: str
    units_per_pack: int
    # True where the pack holds countable articles — tablets, capsules and
    # general pieces — which are the ones sold one at a time.
    is_countable: bool
    schedule_type: ScheduleType
    is_prescription_required: bool
    hsn_code: str | None = None
    # Reference only. `mrp_per_pack` below is what a sale would actually be
    # billed at; this is what the price list said.
    list_mrp_per_pack: Decimal | None = None
    reorder_level_units: Decimal | None = None
    rack_location: str | None = None
    is_active: bool

    # --- read-only, resolved from the batch the POS would sell next -------
    # Null when nothing sellable is in stock, which is the honest answer:
    # the catalogue should not advertise a price nobody can be charged.
    available_units: Decimal = Decimal("0.000")
    batch_count: int = 0
    nearest_expiry: date | None = None
    has_near_expiry: bool = False
    mrp_per_pack: Decimal | None = None
    unit_mrp: Decimal | None = None
    gst_rate: Decimal | None = None
    effective_default_discount_pct: Decimal
    effective_max_discount_pct: Decimal


class MedicineSearchResult(BaseModel):
    """Row shape for the POS search box and the alternatives panel."""

    id: UUID
    brand_name: str
    item_type: ItemType = ItemType.MEDICINE
    composition_id: UUID | None = None
    composition_text: str | None = None
    manufacturer_name: str
    dosage_form: DosageForm
    pack_size: Decimal
    pack_unit: PackUnit
    pack_label: str
    units_per_pack: int
    is_countable: bool
    schedule_type: ScheduleType
    is_prescription_required: bool
    # Live figures from the FEFO batch — the one that would actually be sold.
    # Null price means nothing sellable is in stock.
    mrp_per_pack: Decimal | None = None
    unit_mrp: Decimal | None = None
    gst_rate: Decimal | None = None
    effective_default_discount_pct: Decimal
    effective_max_discount_pct: Decimal
    available_units: Decimal = Decimal("0.000")
    batch_count: int = 0
    nearest_expiry: date | None = None
    has_near_expiry: bool = False
    in_stock: bool = False
    #: True when the row is on the list because it shares a composition with
    #: what was searched for, not because its own name matched. The counter
    #: shows it as a substitute rather than as a hit.
    is_alternative: bool = False


class PackHintOut(BaseModel):
    """A pack size read off the supplier's line, e.g. "30GM" or "15'S"."""

    text: str
    size: Decimal
    #: The PackUnit it implies, or COUNT when the number is a strip count.
    unit: str


class FormHintOut(BaseModel):
    """A dosage form read off the supplier's line, e.g. "TAB" -> TABLET."""

    text: str
    dosage_form: str


class MedicineMatch(BaseModel):
    """One candidate for a line of a supplier's invoice."""

    medicine: MedicineSearchResult
    #: Ordering only. Never a threshold for choosing automatically.
    score: float
    #: False when the name matches but the pack does not — two sizes of the
    #: same brand are two products, and this is what says so.
    pack_matches: bool = True
    #: False when the name matches but the line named a different form — a
    #: tablet is not what "PARACETAMOL SYP" meant, even if both are on file.
    form_matches: bool = True
    note: str | None = None


class MedicineMatchResult(BaseModel):
    """What a supplier's wording might mean, and what was noticed about it.

    Deliberately a *list*, even when there is one obvious answer: a
    distributor's abbreviation is not evidence enough to bind money to a row,
    so the operator chooses.
    """

    query: str
    #: The words actually searched for, after form words came off.
    terms: list[str] = []
    pack_hint: PackHintOut | None = None
    form_hint: FormHintOut | None = None
    matches: list[MedicineMatch] = []
    #: True when some candidate is the pack the line asked for. False with a
    #: `pack_hint` set means: this size is not on the master — add it.
    has_pack_match: bool = False
    #: True when some candidate is the form the line named. False with a
    #: `form_hint` set means: this form is not on the master — add it.
    has_form_match: bool = False


class ImportRowError(BaseModel):
    row: int
    field: str | None = None
    message: str


class ImportResult(BaseModel):
    total_rows: int
    created: int
    updated: int
    skipped: int
    errors: list[ImportRowError] = []
    # Things the file did not say and the importer had to decide. Reported
    # rather than buried, because they are what someone has to check.
    warnings: list[str] = []
    #: Which column layout the file was read as.
    layout: str | None = None
