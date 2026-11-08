"""Goods inward: the supplier's invoice and the lines that become batches.

A purchase invoice is the shop's record of a document someone else issued,
so it is never edited into a different shape once posted — a mistaken entry
is *cancelled*, which reverses the stock through the ledger and leaves both
the original figures and the reversal on record (CLAUDE.md rule 5).

Every line snapshots the pack, the cost, the MRP and the GST rate as they
were on the day the goods arrived. Later edits to the medicine master or to
a batch must never rewrite what a supplier actually billed.
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
from app.db.types import UUIDPK, Money, Quantity, Rate
from app.models.enums import PurchasePaymentStatus, values
from app.models.mixins import ActorMixin, PKMixin, StoreScopedMixin, TimestampMixin


class PurchaseInvoice(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """One supplier invoice received into stock."""

    __tablename__ = "purchase_invoices"
    __table_args__ = (
        # The same supplier cannot bill the same invoice number twice; this is
        # what stops a consignment being received into stock a second time.
        UniqueConstraint(
            "store_id", "supplier_id", "supplier_invoice_no",
            name="uq_purchase_invoices_store_id_supplier_id_supplier_invoice_no",
        ),
        UniqueConstraint(
            "store_id", "doc_number", name="uq_purchase_invoices_store_id_doc_number"
        ),
        Index("ix_purchase_invoices_store_id_received_date", "store_id", "received_date"),
        CheckConstraint(
            f"payment_status IN ({', '.join(repr(v) for v in values(PurchasePaymentStatus))})",
            name="payment_status_valid",
        ),
    )

    supplier_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("suppliers.id"), nullable=False, index=True
    )

    # The supplier's own reference, and ours. Both are needed: theirs for
    # reconciliation and the GST inward register, ours for a gap-free
    # internal sequence.
    supplier_invoice_no: Mapped[str] = mapped_column(String(50), nullable=False)
    supplier_invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    doc_number: Mapped[str] = mapped_column(String(8), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Tax split is decided once, at receipt, from the two state codes.
    is_intra_state: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    subtotal_taxable: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    other_charges: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    round_off: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    total_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    payment_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PurchasePaymentStatus.UNPAID.value
    )
    notes: Mapped[str | None] = mapped_column(Text)

    # Cancellation is recorded, not erased.
    is_cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("users.id"))
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    supplier = relationship("Supplier", back_populates="purchases")
    items: Mapped[list["PurchaseItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="PurchaseItem.id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PurchaseInvoice {self.doc_number} supplier={self.supplier_id}>"


class PurchaseItem(PKMixin, TimestampMixin, Base):
    """One line of a supplier invoice — one medicine, one batch, one expiry."""

    __tablename__ = "purchase_items"
    __table_args__ = (
        Index("ix_purchase_items_medicine_id_expiry_date", "medicine_id", "expiry_date"),
        CheckConstraint("units_per_pack >= 1", name="units_per_pack_positive"),
    )

    purchase_invoice_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("purchase_invoices.id"), nullable=False, index=True
    )
    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )
    # The batch this line landed in. Nullable only for the instant between
    # flushing the line and creating the batch; the FK points this way (not
    # batches -> purchase_items) because a repeat consignment of the same
    # lot tops up one batch from several invoice lines.
    batch_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("batches.id"), index=True
    )

    batch_no: Mapped[str] = mapped_column(String(60), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Pack snapshot, taken from the medicine master at receipt.
    units_per_pack: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    qty_packs: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)
    # Scheme goods: they arrive and are sellable, but are not billed.
    free_qty_packs: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("0.000")
    )
    # Total units taken into stock, free goods included.
    qty_units: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)

    cost_per_pack: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    mrp_per_pack: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    # Trade discount the supplier allowed on this line.
    discount_pct: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("0.00"))
    gst_rate: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("12.00"))

    taxable_value: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    invoice: Mapped[PurchaseInvoice] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PurchaseItem {self.id} med={self.medicine_id} {self.batch_no!r}>"
