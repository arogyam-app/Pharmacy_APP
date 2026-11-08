"""Product catalogue: salts, compositions, manufacturers, medicines.

Salts, compositions and manufacturers are *global* reference data shared by
every store; only `medicines` is store-scoped, because pricing, discount
policy and reorder levels are a shop's own decisions.

The alternative-suggestion feature hangs entirely off `composition_id`: two
medicines are substitutes for each other exactly when they share one.
"""

from uuid import UUID

from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import UUIDPK, Money, Quantity
from app.models.enums import DosageForm, ItemType, PackUnit, ScheduleType, values
from app.models.mixins import ActorMixin, PKMixin, SoftDeleteMixin, TimestampMixin


class Salt(PKMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    """A single active ingredient, e.g. "PARACETAMOL"."""

    __tablename__ = "salts"
    __table_args__ = (UniqueConstraint("name", name="uq_salts_name"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    therapeutic_class: Mapped[str | None] = mapped_column(String(150))

    compositions: Mapped[list["CompositionSalt"]] = relationship(back_populates="salt")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Salt {self.name!r}>"


class Composition(PKMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    """A full formulation — one salt or a fixed-dose combination.

    `composition_text` is canonical: components are uppercased and sorted, so
    "B 5MG + A 10MG" and "A 10MG + B 5MG" collapse to one row and therefore
    one set of alternatives.
    """

    __tablename__ = "compositions"
    __table_args__ = (UniqueConstraint("composition_text", name="uq_compositions_composition_text"),)

    # 1000, not 400: a multi-strain probiotic or an amino-acid blend can list
    # a dozen-plus ingredients and comfortably clear 400 characters.
    composition_text: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    # Same text with every space removed. Lets "paracetamol 500 mg" find
    # "PARACETAMOL 500MG", so a stray space or a capital letter cannot spawn
    # a duplicate formulation — and duplicates would silently split a set of
    # alternatives in two.
    search_key: Mapped[str] = mapped_column(String(1000), nullable=False, default="", index=True)
    therapeutic_class: Mapped[str | None] = mapped_column(String(150))
    notes: Mapped[str | None] = mapped_column(Text)

    components: Mapped[list["CompositionSalt"]] = relationship(
        back_populates="composition", cascade="all, delete-orphan", order_by="CompositionSalt.id"
    )
    medicines: Mapped[list["Medicine"]] = relationship(back_populates="composition")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Composition {self.composition_text!r}>"


class CompositionSalt(PKMixin, TimestampMixin, Base):
    """Join row. Lets a search for one generic name find every combination
    that contains it."""

    __tablename__ = "composition_salts"
    __table_args__ = (
        UniqueConstraint("composition_id", "salt_id", name="uq_composition_salts_composition_id_salt_id"),
    )

    composition_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("compositions.id"), nullable=False, index=True
    )
    salt_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("salts.id"), nullable=False, index=True
    )
    strength: Mapped[str | None] = mapped_column(String(50))

    composition: Mapped[Composition] = relationship(back_populates="components")
    salt: Mapped[Salt] = relationship(back_populates="compositions")


class Manufacturer(PKMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    __tablename__ = "manufacturers"
    __table_args__ = (UniqueConstraint("name", name="uq_manufacturers_name"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    address: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(15))
    email: Mapped[str | None] = mapped_column(String(160))

    medicines: Mapped[list["Medicine"]] = relationship(back_populates="manufacturer")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Manufacturer {self.name!r}>"


class Medicine(PKMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    """A branded pack the shop sells. Stock lives in `batches` (Phase 3) —
    this row carries only the catalogue and policy attributes."""

    __tablename__ = "medicines"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "brand_name",
            "manufacturer_id",
            "pack_label",
            name="uq_medicines_store_id_brand_name_manufacturer_id_pack_label",
        ),
        Index("ix_medicines_store_id_brand_name", "store_id", "brand_name"),
        Index("ix_medicines_store_id_composition_id", "store_id", "composition_id"),
        CheckConstraint(
            f"dosage_form IN ({', '.join(repr(v) for v in values(DosageForm))})",
            name="dosage_form_valid",
        ),
        CheckConstraint(
            f"schedule_type IN ({', '.join(repr(v) for v in values(ScheduleType))})",
            name="schedule_type_valid",
        ),
        CheckConstraint(
            f"pack_unit IN ({', '.join(repr(v) for v in values(PackUnit))})",
            name="pack_unit_valid",
        ),
        CheckConstraint(
            f"item_type IN ({', '.join(repr(v) for v in values(ItemType))})",
            name="item_type_valid",
        ),
        # A medicine is its formulation — without one there are no
        # alternatives, no generic search and nothing to check a
        # prescription against. A general item has none by definition.
        CheckConstraint(
            "item_type <> 'MEDICINE' OR composition_id IS NOT NULL",
            name="medicine_has_composition",
        ),
        CheckConstraint("units_per_pack >= 1", name="units_per_pack_positive"),
    )

    store_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("stores.id"), nullable=False, index=True
    )
    brand_name: Mapped[str] = mapped_column(String(200), nullable=False)
    manufacturer_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("manufacturers.id"), nullable=False, index=True
    )
    #: MEDICINE or GENERAL. Everything a shop *sells* lives in this table;
    #: this column says which of the pharmacological attributes below mean
    #: anything for the row.
    item_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ItemType.MEDICINE.value, index=True
    )
    #: NULL only for a general item — soap has no salt.
    composition_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("compositions.id"), index=True
    )

    dosage_form: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DosageForm.TABLET.value
    )

    # --- how the pack is measured ---------------------------------------
    # "Strip of 10" is a tablet/capsule idea only. `pack_unit` says what the
    # pack is measured in (TABLET, ML, GM, VIAL…) and `pack_size` how much of
    # it there is: 10 tablets, 100 ml, 20 g. Both are set from the dosage
    # form by services.catalog.resolve_pack().
    pack_unit: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PackUnit.TABLET.value
    )
    pack_size: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("1.000")
    )
    # Human label, e.g. "10 tab strip" or "100 ml bottle". Auto-composed from
    # the two fields above when the operator leaves it blank.
    pack_label: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    # Sellable units in one pack: 10 for a strip of ten tablets, 1 for a
    # bottle or vial (nobody sells half a syrup). Stock is always counted in
    # these units, so this is the only pack <-> unit conversion there is.
    units_per_pack: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    schedule_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ScheduleType.OTC.value
    )
    is_prescription_required: Mapped[bool] = mapped_column(nullable=False, default=False)

    # HSN is a product classification — stable, so it belongs to the master.
    # MRP, GST rate and discount limits deliberately do *not* live here: they
    # change from consignment to consignment and are carried by `batches`.
    hsn_code: Mapped[str | None] = mapped_column(String(10))

    # The price the manufacturer's list quoted, kept only as a reference: it
    # is what a bulk price-list upload carries, and what the shop sees for a
    # medicine catalogued before its first delivery. **Nothing prices a sale
    # from this.** Billing MRP comes from the batch being sold, because that
    # is the figure printed on the box the customer is handed — which is why
    # `medicines.mrp_per_pack` was dropped in migration 0004. This column
    # only seeds the MRP field when a batch is entered.
    list_mrp_per_pack: Mapped[Decimal | None] = mapped_column(Money())

    reorder_level_units: Mapped[Decimal | None] = mapped_column(Quantity())
    # Snapshot of stock right after the last goods-inward; the order book
    # measures its 60 % decline against this (Phase 6).
    stock_baseline_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )

    rack_location: Mapped[str | None] = mapped_column(String(50))
    # Lowercased "brand + composition + salts + manufacturer", refreshed on
    # every save. One column to search instead of four joins.
    search_text: Mapped[str] = mapped_column(String(600), nullable=False, default="", index=True)

    manufacturer: Mapped[Manufacturer] = relationship(back_populates="medicines")
    composition: Mapped[Composition | None] = relationship(back_populates="medicines")
    batches = relationship("Batch", back_populates="medicine")

    @property
    def is_general(self) -> bool:
        return self.item_type == ItemType.GENERAL.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Medicine {self.id} {self.brand_name!r}>"
