"""Store profile and per-store settings."""

from uuid import UUID

from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.models.enums import GstRegistrationType, PrintFormat
from app.schemas.common import ORMModel

# A logo or QR arrives base64-encoded inside a data URI. 512 KB of text is
# roughly a 380 KB image — ample for a letterhead, and small enough that a
# bill still renders instantly.
MAX_IMAGE_CHARS = 512 * 1024


class StoreOut(ORMModel):
    id: UUID
    name: str
    legal_name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    state_code: str | None = None
    pincode: str | None = None
    gstin: str | None = None
    drug_licence_no_20b: str | None = None
    drug_licence_no_21b: str | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool


class StoreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    legal_name: str | None = Field(default=None, max_length=200)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    state_code: str | None = Field(default=None, pattern=r"^\d{2}$")
    pincode: str | None = Field(default=None, max_length=10)
    gstin: str | None = Field(default=None, max_length=15)
    drug_licence_no_20b: str | None = Field(default=None, max_length=50)
    drug_licence_no_21b: str | None = Field(default=None, max_length=50)
    phone: str | None = Field(default=None, max_length=15)
    email: EmailStr | None = None


class SettingsOut(ORMModel):
    id: UUID
    store_id: UUID
    default_discount_pct: Decimal
    max_discount_pct: Decimal
    gst_enabled: bool
    gst_registration_type: GstRegistrationType
    price_includes_gst: bool
    near_expiry_days: int
    low_stock_units: Decimal
    order_book_decline_pct: Decimal
    return_window_days: int
    credit_note_validity_days: int
    invoice_prefix: str | None = None
    invoice_footer_text: str | None = None
    print_format: PrintFormat
    round_off_enabled: bool
    allow_negative_stock: bool
    # Letterhead branding, printed by the A4 layout.
    logo_image: str | None = None
    partner_logo_image: str | None = None
    order_qr_image: str | None = None
    order_qr_caption: str | None = None


class SettingsUpdate(BaseModel):
    """Partial update — only the supplied fields change."""

    default_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_discount_pct: Decimal | None = Field(default=None, ge=0, le=100)
    gst_enabled: bool | None = None
    gst_registration_type: GstRegistrationType | None = None
    price_includes_gst: bool | None = None
    near_expiry_days: int | None = Field(default=None, ge=1, le=730)
    low_stock_units: Decimal | None = Field(default=None, ge=0)
    order_book_decline_pct: Decimal | None = Field(default=None, ge=1, le=99)
    return_window_days: int | None = Field(default=None, ge=0, le=365)
    credit_note_validity_days: int | None = Field(default=None, ge=1, le=3650)
    invoice_prefix: str | None = Field(default=None, max_length=10)
    invoice_footer_text: str | None = Field(default=None, max_length=500)
    print_format: PrintFormat | None = None
    round_off_enabled: bool | None = None
    allow_negative_stock: bool | None = None
    # An empty string clears an image; a data URI or a plain URL sets one.
    logo_image: str | None = Field(default=None, max_length=MAX_IMAGE_CHARS)
    partner_logo_image: str | None = Field(default=None, max_length=MAX_IMAGE_CHARS)
    order_qr_image: str | None = Field(default=None, max_length=MAX_IMAGE_CHARS)
    order_qr_caption: str | None = Field(default=None, max_length=80)

    @field_validator("logo_image", "partner_logo_image", "order_qr_image")
    @classmethod
    def _an_image_the_browser_will_load(cls, value: str | None) -> str | None:
        """Only an inline image or an ordinary URL.

        The value is rendered into an `<img src>` on the printed bill, so
        anything else — a `javascript:` URL above all — has no business
        being stored here.
        """
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        if text.startswith("data:image/"):
            return text
        if text.startswith(("http://", "https://", "/")):
            return text
        raise ValueError(
            "Provide an image file, or a link starting with https://."
        )

    @model_validator(mode="after")
    def _default_not_above_max(self) -> "SettingsUpdate":
        if (
            self.default_discount_pct is not None
            and self.max_discount_pct is not None
            and self.default_discount_pct > self.max_discount_pct
        ):
            raise ValueError("Default discount cannot exceed the maximum discount.")
        return self
