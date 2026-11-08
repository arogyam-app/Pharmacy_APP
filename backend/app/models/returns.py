"""Undoing a sale: voids, returns and the credit notes they issue.

A sale is never edited or deleted. There are exactly two ways back:

* **Void** — the whole invoice was a mistake. A sales executive *requests*
  it, an admin approves, and every unit goes back to the batch it came
  from. The invoice number is kept and marked void, never reused.
* **Return** — the customer brings goods back, in whole or in part. The
  refund is the line's post-discount value pro-rata, with GST reversed at
  the rate snapshotted on the original sale, and it settles either as cash
  or as a credit note redeemable on a later visit.

Both leave the original invoice standing (CLAUDE.md rule 5).
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import UUIDPK, Money, Quantity, Rate, UnitPrice
from app.models.enums import (
    ApprovalStatus,
    CreditNoteStatus,
    ItemCondition,
    RefundMode,
    ReturnStatus,
    values,
)
from app.models.mixins import ActorMixin, PKMixin, StoreScopedMixin, TimestampMixin


class SaleVoidRequest(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """A request to cancel an invoice outright, awaiting an admin.

    The two-step approval is the whole point: the person who billed the
    sale cannot also erase it. That separation is what makes the register
    trustworthy — otherwise a till shortfall could be covered by voiding
    the sale that caused it.
    """

    __tablename__ = "sale_void_requests"
    __table_args__ = (
        Index("ix_sale_void_requests_store_id_status", "store_id", "status"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(ApprovalStatus))})",
            name="status_valid",
        ),
    )

    sale_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("sales.id"), nullable=False, index=True
    )
    requested_by_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("users.id"), nullable=False
    )
    requested_by_name: Mapped[str | None] = mapped_column(String(150))
    request_reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ApprovalStatus.PENDING.value
    )
    reviewed_by_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("users.id"))
    reviewed_by_name: Mapped[str | None] = mapped_column(String(150))
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SaleVoidRequest sale={self.sale_id} {self.status}>"


class SaleReturn(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """Goods coming back against one invoice."""

    __tablename__ = "sale_returns"
    __table_args__ = (
        UniqueConstraint("store_id", "return_no", name="uq_sale_returns_store_id_return_no"),
        Index("ix_sale_returns_store_id_return_date", "store_id", "return_date"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(ReturnStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            f"refund_mode IN ({', '.join(repr(v) for v in values(RefundMode))})",
            name="refund_mode_valid",
        ),
    )

    return_no: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    sale_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("sales.id"), nullable=False, index=True
    )
    return_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text)

    refund_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RefundMode.CREDIT_NOTE.value
    )

    gross_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    discount_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    taxable_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    total_refund_amount: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0")
    )

    processed_by_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("users.id"), nullable=False
    )
    processed_by_name: Mapped[str | None] = mapped_column(String(150))
    # Required once the return window has closed: an old return is an
    # owner's decision, not a counter one.
    approved_by_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("users.id"))
    approved_by_name: Mapped[str | None] = mapped_column(String(150))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ReturnStatus.COMPLETED.value, index=True
    )
    # False when nothing came back in saleable condition.
    restocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    days_since_sale: Mapped[int] = mapped_column(nullable=False, default=0)

    items: Mapped[list["SaleReturnItem"]] = relationship(
        back_populates="sale_return", cascade="all, delete-orphan", order_by="SaleReturnItem.id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SaleReturn {self.return_no} {self.total_refund_amount}>"


class SaleReturnItem(PKMixin, TimestampMixin, Base):
    """One returned line, traced to the sale line it came from."""

    __tablename__ = "sale_return_items"
    __table_args__ = (
        CheckConstraint(
            f"condition IN ({', '.join(repr(v) for v in values(ItemCondition))})",
            name="condition_valid",
        ),
    )

    sale_return_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("sale_returns.id"), nullable=False, index=True
    )
    # The line being returned against; this is what caps the quantity.
    sale_item_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("sale_items.id"), nullable=False, index=True
    )
    medicine_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("medicines.id"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("batches.id"), nullable=False, index=True
    )
    medicine_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    batch_no_snapshot: Mapped[str] = mapped_column(String(60), nullable=False)

    qty_units: Mapped[Decimal] = mapped_column(Quantity(), nullable=False)
    unit_mrp: Mapped[Decimal] = mapped_column(UnitPrice(), nullable=False)
    discount_pct: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("0"))
    # Snapshotted from the sale line: tax is reversed at the rate that was
    # charged, not whatever the rate happens to be today.
    gst_rate: Mapped[Decimal] = mapped_column(Rate(), nullable=False, default=Decimal("0"))

    taxable_value: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    cgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    sgst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    igst_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    refund_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))

    # SALEABLE goes back on the shelf; DAMAGED and EXPIRED do not.
    condition: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ItemCondition.SALEABLE.value
    )
    restocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    sale_return: Mapped[SaleReturn] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SaleReturnItem {self.medicine_name_snapshot!r} x{self.qty_units}>"


class CreditNote(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, Base):
    """Money owed back to a customer, redeemable against a later sale.

    A credit note belongs to a customer, so an anonymous sale cannot have
    one — there would be no way to know who is entitled to spend it. The
    counter takes a mobile number before issuing.
    """

    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint(
            "store_id", "credit_note_no", name="uq_credit_notes_store_id_credit_note_no"
        ),
        Index("ix_credit_notes_store_id_customer_id_status", "store_id", "customer_id", "status"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in values(CreditNoteStatus))})",
            name="status_valid",
        ),
        # Same-scale column comparison — dialect-safe, unlike a numeric
        # literal (see app/db/types.py).
        CheckConstraint("amount_used <= amount_issued", name="used_not_above_issued"),
    )

    credit_note_no: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    customer_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("customers.id"), nullable=False, index=True
    )
    sale_return_id: Mapped[UUID | None] = mapped_column(
        UUIDPK(), ForeignKey("sale_returns.id"), index=True
    )
    issue_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    amount_issued: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    amount_used: Mapped[Decimal] = mapped_column(Money(), nullable=False, default=Decimal("0"))
    # Kept in step with the usages; a test asserts issued − used == balance.
    balance_amount: Mapped[Decimal] = mapped_column(Money(), nullable=False)

    expires_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CreditNoteStatus.OPEN.value, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    cancelled_reason: Mapped[str | None] = mapped_column(Text)

    usages: Mapped[list["CreditNoteUsage"]] = relationship(
        back_populates="credit_note", order_by="CreditNoteUsage.id"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CreditNote {self.credit_note_no} bal={self.balance_amount}>"


class CreditNoteUsage(PKMixin, TimestampMixin, ActorMixin, Base):
    """One redemption. Append-only, so a note's history is reconstructible."""

    __tablename__ = "credit_note_usages"

    credit_note_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("credit_notes.id"), nullable=False, index=True
    )
    sale_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("sales.id"), nullable=False, index=True
    )
    payment_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("payments.id"))
    amount_used: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    credit_note: Mapped[CreditNote] = relationship(back_populates="usages")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CreditNoteUsage note={self.credit_note_id} {self.amount_used}>"
