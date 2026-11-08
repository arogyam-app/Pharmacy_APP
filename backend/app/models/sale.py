"""The sale, its lines and its payments — the shop's financial record.

Three rules shape this module:

* **Nothing here is ever deleted.** There is no delete path in the codebase
  for a sale, a line or a payment; a mistake becomes a void (Phase 5) or a
  return, both of which leave the original document standing (CLAUDE.md
  rule 5).
* **Everything is snapshotted.** Brand name, batch number, expiry, MRP, GST
  rate and unit cost are copied onto the line at the moment of sale. Editing
  the catalogue or a batch afterwards must never rewrite an invoice that has
  already been printed and handed over.
* **One line per batch.** A cart line that spans two batches becomes two
  `sale_items`, because each carries a different expiry and possibly a
  different MRP, and the ledger must be able to give each batch its units
  back.
"""

from uuid import UUID

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import UUIDPK, Money, Quantity, Rate, UnitPrice
from app.models.enums import ItemType, PaymentMode, SaleStatus, values
from app.models.mixins import ActorMixin, PKMixin, StoreScopedMixin, TimestampMixin


class Sale(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """One invoice."""

    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("store_id", "invoice_no", name="uq_sales_store_id_invoice_no"),
        # A repeated submit — a double-click, a retried request — must return
        # the first invoice rather than issue a second one.
        UniqueConstraint("idempotency_key", name="uq_sales_idempotency_key"),
        Index("ix_sales_store_id_invoice_date", "store_id", "invoice_date"),
        # The register searches by mobile, including for walk-ins who have no
        # customer row.
        Index("ix_sales_store_id_customer_mobile_snapshot",
              "store_id", "customer_mobile_snapshot"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(SaleStatus))})",
            name="status_valid",
        ),
    )

    invoice_no: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    invoice_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    customer_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("customers.id"), index=True
    )
    # Snapshots, so a walk-in sale is still findable and a later change to
    # the customer master does not rewrite old invoices.
    customer_mobile_snapshot: Mapped[str | None] = mapped_column(String(15))
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(150))
    customer_gstin_snapshot: Mapped[str | None] = mapped_column(String(15))

    doctor_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("doctors.id"))
    doctor_name_snapshot: Mapped[str | None] = mapped_column(String(150))
    prescription_ref: Mapped[str | None] = mapped_column(String(100))

    sales_user_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("users.id"), nullable=False, index=True
    )
    sales_user_name: Mapped[str | None] = mapped_column(String(150))

    gross_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    taxable_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    round_off: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    net_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    paid_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=SaleStatus.COMPLETED.value, index=True
    )
    is_intra_state: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reprint_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    # Set when an admin billed past the normal discount ceiling.
    discount_override_reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["SaleItem"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", order_by="SaleItem.line_no"
    )
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", order_by="Payment.id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Sale {self.invoice_no} {self.net_amount} {self.status}>"


class SaleItem(PKMixin, TimestampMixin, Base):
    """One line: one medicine drawn from one batch."""

    __tablename__ = "sale_items"
    __table_args__ = (
        Index("ix_sale_items_batch_id_sale_id", "batch_id", "sale_id"),
        # Same-scale column comparison — safe on both dialects, unlike a
        # numeric literal (see app/db/types.py).
        CheckConstraint("returned_qty_units <= qty_units", name="returned_not_above_sold"),
        CheckConstraint(
            f"item_type_snapshot IN ({', '.join(repr(v) for v in values(ItemType))})",
            name="sale_items_item_type_snapshot_valid",
        ),
    )

    sale_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("sales.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("batches.id"), nullable=False, index=True
    )

    # --- snapshots: what the customer was actually handed ----------------
    medicine_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    pack_label_snapshot: Mapped[str | None] = mapped_column(String(60))
    hsn_code_snapshot: Mapped[str | None] = mapped_column(String(10))
    # MEDICINE or GENERAL, as of the sale. Not print trivia: a bill shows
    # HSN and batch for a medicine and neither for a bar of soap, and that
    # has to be decided from what was actually sold, not from what the
    # catalogue says today.
    item_type_snapshot: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ItemType.MEDICINE.value
    )
    batch_no_snapshot: Mapped[str] = mapped_column(String(60), nullable=False)
    expiry_date_snapshot: Mapped[date] = mapped_column(Date, nullable=False)
    units_per_pack_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    qty_units: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)
    unit_mrp: Mapped[Decimal] = mapped_column(UnitPrice(), nullable=False)
    mrp_per_pack: Mapped[Decimal] = mapped_column(Money(), nullable=False)

    gross_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    discount_pct: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    net_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    gst_rate: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("0"))
    taxable_value: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    returned_qty_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    # Admin-only in every response. Frozen here so a later price change
    # cannot rewrite the margin this sale actually earned.
    unit_cost_snapshot: Mapped[Decimal | None] = mapped_column(UnitPrice())

    sale: Mapped[Sale] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SaleItem {self.medicine_name_snapshot!r} x{self.qty_units}>"


class Payment(PKMixin, TimestampMixin, ActorMixin, Base):
    """How an invoice was settled. Several rows mean a split payment."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            f"mode IN ({', '.join(repr(v) for v in values(PaymentMode))})", name="mode_valid"
        ),
    )

    sale_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("sales.id"), nullable=False, index=True
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    # Card last-4, UPI reference, cheque number — whatever ties the money to
    # the bank statement.
    reference_no: Mapped[str | None] = mapped_column(String(60))
    # Populated when the mode is CREDIT_NOTE (Phase 5).
    credit_note_id: Mapped[UUID | None] = mapped_column(UUIDPK())
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    sale: Mapped[Sale] = relationship(back_populates="payments")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Payment {self.mode} {self.amount}>"
