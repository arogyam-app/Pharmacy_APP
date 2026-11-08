"""Inventory: batches and the stock ledger.

Commercial terms live here, not on the medicine master, because they change
with every consignment: a new batch arrives with its own printed MRP, its own
GST rate if the slab moved, and possibly its own discount ceiling (a
near-expiry lot is often cleared at a deeper discount).

`batches.qty_available_units` is a cached projection of `stock_ledger`; the
ledger is the source of truth and a test asserts the two agree.
"""

from uuid import UUID

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import UUIDPK, Money, Quantity, Rate, UnitPrice
from app.models.enums import (
    NO_EXPIRY,
    ExpiryBasis,
    MovementType,
    PackUnit,
    ReferenceType,
    values,
)
from app.models.mixins import ActorMixin, PKMixin, TimestampMixin


class Batch(PKMixin, TimestampMixin, ActorMixin, Base):
    """One consignment of one medicine, with its own price, tax and expiry."""

    __tablename__ = "batches"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "medicine_id", "batch_no", "expiry_date",
            name="uq_batches_store_id_medicine_id_batch_no_expiry_date",
        ),
        # Drives FEFO: oldest expiry first.
        Index("ix_batches_store_id_medicine_id_expiry_date",
              "store_id", "medicine_id", "expiry_date"),
        CheckConstraint(
            f"pack_unit IN ({', '.join(repr(v) for v in values(PackUnit))})",
            name="pack_unit_valid",
        ),
        CheckConstraint("units_per_pack >= 1", name="units_per_pack_positive"),
        # Same-scale column comparison — safe on both dialects, unlike a
        # numeric literal (see app/db/types.py).
        CheckConstraint(
            "qty_available_units <= qty_received_units", name="available_not_above_received"
        ),
        CheckConstraint(
            f"expiry_basis IN ({', '.join(repr(v) for v in values(ExpiryBasis))})",
            name="expiry_basis_valid",
        ),
    )

    store_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("stores.id"), nullable=False, index=True
    )
    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )

    batch_no: Mapped[str] = mapped_column(String(60), nullable=False)
    #: Always set, whatever the pack printed. See `expiry_basis` — for stock
    #: that never expires this holds `enums.NO_EXPIRY`, which sorts last
    #: under FEFO and matches no near-expiry window.
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Where `expiry_date` came from: printed on the pack, computed from the
    #: manufacturing date, or nothing at all.
    expiry_basis: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExpiryBasis.EXPIRY_DATE.value
    )
    #: Both set only for BEST_BEFORE. Kept rather than discarded after the
    #: computation, because the pack says "MFG 03/2026, best before 24
    #: months" and a stock check at the shelf has to reconcile with that.
    mfg_date: Mapped[date | None] = mapped_column(Date)
    best_before_months: Mapped[int | None] = mapped_column(Integer)

    # Pack snapshot: the master may be edited later, but what arrived in this
    # consignment must not change retrospectively.
    pack_unit: Mapped[str] = mapped_column(String(20), nullable=False)
    pack_size: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)
    units_per_pack: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    qty_received_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    # Cached projection of the ledger — never write it without a ledger row.
    qty_available_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )

    # --- commercial terms, per consignment ------------------------------
    mrp_per_pack: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    unit_mrp: Mapped[Decimal] = mapped_column(UnitPrice(), nullable=False)
    # Cost never leaves the API for a non-admin (CLAUDE.md rule 4).
    cost_per_pack: Mapped[Decimal | None] = mapped_column(Money())
    unit_cost: Mapped[Decimal | None] = mapped_column(UnitPrice())

    gst_rate: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("12.00"))
    # NULL falls back to the store policy (10 % / 20 %).
    default_discount_pct: Mapped[Decimal | None] = mapped_column(Rate())
    max_discount_pct: Mapped[Decimal | None] = mapped_column(Rate())

    received_date: Mapped[date | None] = mapped_column(Date)
    # Who supplied this lot. Set on first receipt and left alone afterwards:
    # a repeat consignment of the same lot tops the batch up, and the batch
    # keeps the origin it was opened with. NULL for opening stock.
    supplier_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("suppliers.id"), index=True
    )

    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    block_reason: Mapped[str | None] = mapped_column(String(200))

    medicine = relationship("Medicine", back_populates="batches")
    ledger_entries: Mapped[list["StockLedger"]] = relationship(back_populates="batch")

    @property
    def has_expiry(self) -> bool:
        """False for stock that never goes off, whatever `expiry_date` says."""
        return (
            self.expiry_basis != ExpiryBasis.NONE.value and self.expiry_date != NO_EXPIRY
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Batch {self.id} med={self.medicine_id} {self.batch_no!r} exp={self.expiry_date}>"


class StockLedger(PKMixin, TimestampMixin, ActorMixin, Base):
    """Append-only record of every unit that moved. Never updated or deleted."""

    __tablename__ = "stock_ledger"
    __table_args__ = (
        Index("ix_stock_ledger_batch_id_created_at", "batch_id", "created_at"),
        Index("ix_stock_ledger_store_id_medicine_id_created_at",
              "store_id", "medicine_id", "created_at"),
        CheckConstraint(
            f"movement_type IN ({', '.join(repr(v) for v in values(MovementType))})",
            name="movement_type_valid",
        ),
    )

    store_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("stores.id"), nullable=False, index=True
    )
    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("batches.id"), nullable=False, index=True
    )

    movement_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # Signed: positive in, negative out. The sum over a batch must equal
    # batches.qty_available_units.
    qty_units_delta: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)
    balance_after_units: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)

    reference_type: Mapped[str | None] = mapped_column(String(24))
    reference_id: Mapped[UUID | None] = mapped_column(UUIDPK())
    reason: Mapped[str | None] = mapped_column(Text)

    batch: Mapped[Batch] = relationship(back_populates="ledger_entries")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StockLedger {self.movement_type} {self.qty_units_delta:+} batch={self.batch_id}>"
