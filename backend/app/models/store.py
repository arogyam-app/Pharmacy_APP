"""Store (tenant) and its per-store settings row."""

from uuid import UUID

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import UUIDPK, Quantity, Rate
from app.models.enums import GstRegistrationType, PrintFormat, values
from app.models.mixins import PKMixin, SoftDeleteMixin, TimestampMixin


class Store(PKMixin, TimestampMixin, SoftDeleteMixin, Base):
    """One physical outlet. Today there is exactly one; every business row
    already carries `store_id` so adding outlets later is data, not schema.

    Deliberately has no ActorMixin: stores.created_by -> users.id combined
    with users.store_id -> stores.id would be a circular FK.
    """

    __tablename__ = "stores"

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    # Drives intra-state (CGST+SGST) vs inter-state (IGST) on every document.
    state_code: Mapped[str | None] = mapped_column(String(2))
    pincode: Mapped[str | None] = mapped_column(String(10))
    gstin: Mapped[str | None] = mapped_column(String(15))
    drug_licence_no_20b: Mapped[str | None] = mapped_column(String(50))
    drug_licence_no_21b: Mapped[str | None] = mapped_column(String(50))
    phone: Mapped[str | None] = mapped_column(String(15))
    email: Mapped[str | None] = mapped_column(String(160))

    settings: Mapped["StoreSettings"] = relationship(
        back_populates="store", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Store {self.id} {self.name!r}>"


class StoreSettings(PKMixin, TimestampMixin, Base):
    """Every tunable policy for one store. Exactly one row per store.

    Defaults encode the rules from docs/SPEC.md: minimum discount 10 %,
    maximum 20 %, near-expiry 90 days, order book at a 60 % stock decline.
    """

    __tablename__ = "store_settings"
    __table_args__ = (
        UniqueConstraint("store_id", name="uq_store_settings_store_id"),
        # Column-to-column comparison of two same-scale PreciseDecimal columns
        # is dialect-safe. A comparison against a *literal* would not be: on
        # SQLite these columns hold scaled integers (10.00 -> 1000), so
        # `<= 100` would mean 1.00. Range limits live in the Pydantic schema
        # and the settings service instead.
        CheckConstraint(
            "default_discount_pct <= max_discount_pct", name="default_not_above_max"
        ),
        CheckConstraint(
            f"gst_registration_type IN ({', '.join(repr(v) for v in values(GstRegistrationType))})",
            name="gst_registration_type_valid",
        ),
        CheckConstraint(
            f"print_format IN ({', '.join(repr(v) for v in values(PrintFormat))})",
            name="print_format_valid",
        ),
    )

    store_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("stores.id"), nullable=False, index=True
    )

    # --- discount policy -------------------------------------------------
    default_discount_pct: Mapped[Decimal] = mapped_column(
        Rate(), nullable=False, default=Decimal("10.00")
    )
    max_discount_pct: Mapped[Decimal] = mapped_column(
        Rate(), nullable=False, default=Decimal("20.00")
    )

    # --- gst -------------------------------------------------------------
    gst_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    gst_registration_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=GstRegistrationType.REGULAR.value
    )
    price_includes_gst: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- alert thresholds ------------------------------------------------
    near_expiry_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    low_stock_units: Mapped[Decimal] = mapped_column(
        Quantity(), nullable=False, default=Decimal("10.000")
    )
    order_book_decline_pct: Mapped[Decimal] = mapped_column(
        Rate(), nullable=False, default=Decimal("60.00")
    )

    # --- returns ---------------------------------------------------------
    return_window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    credit_note_validity_days: Mapped[int] = mapped_column(Integer, nullable=False, default=180)

    # --- invoicing / printing -------------------------------------------
    invoice_prefix: Mapped[str | None] = mapped_column(String(10))
    invoice_footer_text: Mapped[str | None] = mapped_column(String(500))
    print_format: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PrintFormat.A4.value
    )

    # --- letterhead branding ---------------------------------------------
    # Held as data URIs (or plain URLs) rather than uploaded files: a shop
    # sets these once, and keeping them in settings means the bill needs no
    # filesystem and no second request to render.
    logo_image: Mapped[str | None] = mapped_column(Text)
    partner_logo_image: Mapped[str | None] = mapped_column(Text)
    order_qr_image: Mapped[str | None] = mapped_column(Text)
    order_qr_caption: Mapped[str | None] = mapped_column(String(80))
    round_off_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_negative_stock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    store: Mapped[Store] = relationship(back_populates="settings")
