"""Watching stock: what has expired, and what needs reordering.

Two registers that both answer "what should the owner do about this?":

* `expiry_register` — batches that crossed their date with stock still on
  hand. Recording them is what turns a silent loss into a decision: write it
  off, or send it back to the supplier for credit.
* `order_book` — medicines whose stock has fallen far enough to reorder,
  raised automatically against a baseline taken at the last goods-inward
  (SPEC §3.9).

Neither table holds money the shop has earned, so neither is immutable in
the way a sale is; both are working documents an admin edits.
"""

from uuid import UUID

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base
from app.db.types import UUIDPK, Money, Quantity, Rate
from app.models.enums import (
    ExpirySource,
    ExpiryStatus,
    OrderBookStatus,
    OrderBookTrigger,
    values,
)
from app.models.mixins import ActorMixin, PKMixin, StoreScopedMixin, TimestampMixin


class ExpiryRegisterEntry(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """One expired batch, and what was done about it."""

    __tablename__ = "expiry_register"
    __table_args__ = (
        # One live entry per batch: a repeated scan must not pile up rows for
        # the same expired stock.
        UniqueConstraint("store_id", "batch_id", name="uq_expiry_register_store_id_batch_id"),
        Index("ix_expiry_register_store_id_status", "store_id", "status"),
        CheckConstraint(
            f"source IN ({', '.join(repr(v) for v in values(ExpirySource))})",
            name="source_valid",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(ExpiryStatus))})",
            name="status_valid",
        ),
    )

    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("batches.id"), nullable=False, index=True
    )
    medicine_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    batch_no: Mapped[str] = mapped_column(String(60), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # What was on hand when the entry was raised. The batch may be written
    # off later, so this is the figure the loss is measured on.
    qty_units: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)
    # Admin-only in every response, like every other cost figure.
    cost_value: Mapped[Decimal | None] = mapped_column(Money())
    mrp_value: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExpirySource.EXPIRED_STOCK.value
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ExpiryStatus.IDENTIFIED.value, index=True
    )

    action_date: Mapped[date | None] = mapped_column(Date)
    actioned_by_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("users.id"))
    actioned_by_name: Mapped[str | None] = mapped_column(String(150))

    # Filled when the goods go back to the distributor for credit.
    supplier_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("suppliers.id"))
    debit_note_ref: Mapped[str | None] = mapped_column(String(60))
    credit_received_amount: Mapped[Decimal | None] = mapped_column(Money())

    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ExpiryRegisterEntry {self.batch_no!r} {self.status}>"


class OrderBookEntry(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """A medicine the shop should reorder, and how much of it.

    Raised automatically when stock falls to the decline threshold or below
    the reorder level. At most one entry per medicine is open at a time —
    a second one would just be the same suggestion twice — so an existing
    open row is refreshed rather than duplicated.
    """

    __tablename__ = "order_book"
    __table_args__ = (
        Index("ix_order_book_store_id_status", "store_id", "status"),
        Index("ix_order_book_store_id_medicine_id_status",
              "store_id", "medicine_id", "status"),
        CheckConstraint(
            f"trigger_reason IN ({', '.join(repr(v) for v in values(OrderBookTrigger))})",
            name="trigger_reason_valid",
        ),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(OrderBookStatus))})",
            name="status_valid",
        ),
    )

    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )
    trigger_reason: Mapped[str] = mapped_column(
        String(24), nullable=False, default=OrderBookTrigger.STOCK_DECLINE_60.value
    )

    # The figures the suggestion was based on, kept so the owner can see why
    # the entry exists rather than having to trust it.
    baseline_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    available_units_at_trigger: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    decline_pct: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("0.00"))

    suggested_qty_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    suggested_qty_packs: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    avg_monthly_consumption_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    # None means "no sales to measure against", which is not the same as
    # "runs out today" — the UI shows a dash rather than a zero.
    days_of_cover: Mapped[int | None] = mapped_column(Integer)

    preferred_supplier_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("suppliers.id"), index=True
    )
    last_purchase_cost: Mapped[Decimal | None] = mapped_column(Money())

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OrderBookStatus.OPEN.value, index=True
    )
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purchase_invoice_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("purchase_invoices.id")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrderBookEntry med={self.medicine_id} {self.status} {self.suggested_qty_units}>"
