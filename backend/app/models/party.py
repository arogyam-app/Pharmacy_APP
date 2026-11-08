"""Outside parties the store transacts with: suppliers, customers, doctors.

All three are store-scoped. A distributor's terms and a customer's credit
balance are a shop's own commercial relationship, not shared reference data
like a salt or a manufacturer.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import Money
from app.models.mixins import ActorMixin, PKMixin, SoftDeleteMixin, StoreScopedMixin, TimestampMixin


class Supplier(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    """A distributor or manufacturer the shop buys from.

    `state_code` decides the tax split on every invoice from this supplier:
    same code as the store means CGST + SGST, a different one means IGST.
    """

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("store_id", "name", name="uq_suppliers_store_id_name"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    address: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(15))
    email: Mapped[str | None] = mapped_column(String(160))
    contact_person: Mapped[str | None] = mapped_column(String(120))
    drug_licence_no: Mapped[str | None] = mapped_column(String(50))
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)

    purchases: Mapped[list["PurchaseInvoice"]] = relationship(  # noqa: F821
        back_populates="supplier"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Supplier {self.id} {self.name!r}>"


class Customer(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    """Someone the shop sells to.

    The **mobile number is the identity** — it is what the counter types, what
    the register searches, and what a credit note is matched against. A sale
    to a walk-in carries no customer row at all; the mobile is snapshotted
    onto the invoice instead, so the register still finds it.
    """

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("store_id", "mobile", name="uq_customers_store_id_mobile"),
        Index("ix_customers_store_id_name", "store_id", "name"),
    )

    mobile: Mapped[str] = mapped_column(String(15), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(150))
    address: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(160))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    # Present only for B2B billing; a GSTIN turns the invoice into a tax
    # invoice rather than a retail bill.
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str | None] = mapped_column(String(2))
    # Cached sum of open credit notes (Phase 5); the notes remain the truth.
    credit_balance: Mapped[Decimal] = mapped_column(
        Money(), nullable=False, default=Decimal("0.00")
    )
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Customer {self.id} {self.mobile}>"


class Doctor(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    """The prescriber. Required on a sale that includes a scheduled drug."""

    __tablename__ = "doctors"
    __table_args__ = (Index("ix_doctors_store_id_name", "store_id", "name"),)

    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    registration_no: Mapped[str | None] = mapped_column(String(50))
    specialization: Mapped[str | None] = mapped_column(String(120))
    hospital_clinic: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(15))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Doctor {self.id} {self.name!r}>"
