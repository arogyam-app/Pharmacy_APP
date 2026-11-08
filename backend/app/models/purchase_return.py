"""Goods sent back to a distributor, and the credit that comes of it.

A pharmacy returns stock — a wrong item, an overstock, a near-expiry take-back
— and the distributor settles it by knocking the value off a later invoice.
Under GST that is two documents, not one: this debit note reverses the input
tax credit claimed when the goods came in, and the next purchase invoice
claims its own credit in full. Folding the return into that invoice as
negative lines would net the two into a single figure and neither could be
reported.

The customer side of the shop does the same thing with `SaleReturn` and
`CreditNote`. This is the mirror, with two deliberate differences:

* The debit note carries its own balance rather than issuing a separate credit
  document. A customer credit note has a life of its own — it can be issued
  as goodwill without a return, and it expires. Supplier credit has neither
  property: it exists because goods went back, and a distributor does not time
  out on money they owe.
* There is no refund mode. The credit is only ever set against a future
  invoice from the same distributor, which is what the trade actually does.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import UUIDPK, Money, Quantity, Rate, UnitPrice
from app.models.enums import PurchaseReturnStatus, SupplierCreditStatus, values
from app.models.mixins import ActorMixin, PKMixin, StoreScopedMixin, TimestampMixin


class PurchaseReturn(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """One debit note: stock out, input tax reversed, credit created."""

    __tablename__ = "purchase_returns"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "debit_note_no", name="uq_purchase_returns_store_id_debit_note_no"
        ),
        Index("ix_purchase_returns_store_id_return_date", "store_id", "return_date"),
        Index("ix_purchase_returns_store_id_supplier_id_status",
              "store_id", "supplier_id", "status"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(PurchaseReturnStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            f"credit_status IN ({', '.join(repr(v) for v in values(SupplierCreditStatus))})",
            name="credit_status_valid",
        ),
    )

    #: Ours, gap-free, from `document_sequences`. Goes on the paperwork that
    #: travels back with the goods.
    debit_note_no: Mapped[str] = mapped_column(String(8), nullable=False, index=True)

    supplier_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("suppliers.id"), nullable=False, index=True
    )
    #: The invoice the goods arrived on, where it is known. Optional: stock
    #: bought months ago across several invoices still gets returned, and
    #: forcing a link would mean inventing one.
    purchase_invoice_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("purchase_invoices.id"), index=True
    )

    return_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    #: The distributor's own credit note against this return, once they send
    #: it. Recorded for reconciliation — theirs is the document their books
    #: are built on, ours is the document ours are.
    supplier_credit_note_ref: Mapped[str | None] = mapped_column(String(60))

    #: Fixed at the moment of return from the two state codes, exactly as a
    #: purchase does it. Never recomputed: the supplier could move state.
    is_intra_state: Mapped[bool] = mapped_column(nullable=False, default=True)

    taxable_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    #: taxable + tax. This is the credit the distributor owes.
    total_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PurchaseReturnStatus.COMPLETED.value, index=True
    )

    # --- the credit this note carries -------------------------------------
    #: Kept on the document rather than in a separate table; see the module
    #: docstring. `credit_balance` is the figure the row lock protects, so two
    #: invoices cannot both spend the same rupee.
    credit_used: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    credit_balance: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    credit_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SupplierCreditStatus.OPEN.value, index=True
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("users.id"))
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    notes: Mapped[str | None] = mapped_column(Text)

    supplier = relationship("Supplier")
    items: Mapped[list["PurchaseReturnItem"]] = relationship(
        back_populates="purchase_return",
        cascade="all, delete-orphan",
        order_by="PurchaseReturnItem.line_no",
    )
    usages: Mapped[list["SupplierCreditUsage"]] = relationship(
        back_populates="purchase_return", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PurchaseReturn {self.debit_note_no} supplier={self.supplier_id}>"


class PurchaseReturnItem(PKMixin, TimestampMixin, Base):
    """One line of a debit note — one batch, going back at what it came in at."""

    __tablename__ = "purchase_return_items"
    purchase_return_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("purchase_returns.id"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(nullable=False, default=1)

    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("batches.id"), nullable=False, index=True
    )
    #: The line the goods came in on, where the return was raised from an
    #: invoice rather than from the shelf.
    purchase_item_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("purchase_items.id")
    )

    # Snapshots. A debit note is a financial document and must still read the
    # same after the medicine master is edited (CLAUDE.md rule 5).
    medicine_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    pack_label_snapshot: Mapped[str | None] = mapped_column(String(60))
    hsn_code_snapshot: Mapped[str | None] = mapped_column(String(10))
    batch_no_snapshot: Mapped[str] = mapped_column(String(60), nullable=False)
    expiry_date_snapshot: Mapped[date] = mapped_column(Date, nullable=False)

    qty_units: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)
    #: What the shop paid for one unit, GST-exclusive — goods go back at the
    #: price they came in at, not at MRP. The distributor credits cost.
    cost_per_unit: Mapped[Decimal] = mapped_column(UnitPrice(), nullable=False)

    gst_rate: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("0"))
    taxable_value: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    purchase_return: Mapped[PurchaseReturn] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PurchaseReturnItem {self.batch_no_snapshot} x{self.qty_units}>"


class SupplierCreditUsage(PKMixin, TimestampMixin, ActorMixin, Base):
    """One debit note's credit, set against one later purchase invoice.

    Split out rather than a column on the invoice because a big return can pay
    down several invoices and a big invoice can absorb several returns. The
    row is the evidence for both sides of that.
    """

    __tablename__ = "supplier_credit_usages"
    purchase_return_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("purchase_returns.id"), nullable=False, index=True
    )
    purchase_invoice_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("purchase_invoices.id"), nullable=False, index=True
    )
    amount_used: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    purchase_return: Mapped[PurchaseReturn] = relationship(back_populates="usages")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SupplierCreditUsage {self.amount_used} -> {self.purchase_invoice_id}>"
