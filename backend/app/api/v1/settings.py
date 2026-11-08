"""Store profile and store settings.

Settings are readable by every signed-in user (the POS needs the discount
policy and near-expiry window) but writable only by an admin.
"""

from decimal import Decimal

from fastapi import APIRouter, Request

from app.core.deps import AdminUser, CurrentUser, DbSession, get_store_settings
from app.core.exceptions import AppError, NotFoundError
from app.models.enums import AuditAction
from app.models.store import Store
from app.schemas.store import SettingsOut, SettingsUpdate, StoreOut, StoreUpdate
from app.services import audit

router = APIRouter()

SETTINGS_AUDIT_FIELDS = [
    "default_discount_pct",
    "max_discount_pct",
    "gst_enabled",
    "gst_registration_type",
    "price_includes_gst",
    "near_expiry_days",
    "low_stock_units",
    "order_book_decline_pct",
    "return_window_days",
    "credit_note_validity_days",
    "print_format",
    "round_off_enabled",
    "allow_negative_stock",
    # The images themselves are far too large to snapshot into an audit
    # row; that one was set is what matters.
    "order_qr_caption",
]


@router.get("/settings", response_model=SettingsOut)
def read_settings(user: CurrentUser, db: DbSession) -> SettingsOut:
    return SettingsOut.model_validate(get_store_settings(db, user.store_id))


@router.patch("/settings", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate, request: Request, admin: AdminUser, db: DbSession
) -> SettingsOut:
    row = get_store_settings(db, admin.store_id)
    before = audit.snapshot(row, SETTINGS_AUDIT_FIELDS)
    changes = payload.model_dump(exclude_unset=True)

    # Cross-check against the persisted values, since either side may be
    # absent from a partial update.
    new_default: Decimal = changes.get("default_discount_pct", row.default_discount_pct)
    new_max: Decimal = changes.get("max_discount_pct", row.max_discount_pct)
    if new_default > new_max:
        raise AppError(
            f"Default discount ({new_default}%) cannot exceed the maximum ({new_max}%).",
            code="DISCOUNT_POLICY_INVALID",
            field="default_discount_pct",
        )

    for field, value in changes.items():
        setattr(row, field, value.value if hasattr(value, "value") else value)
    row.updated_by_id = admin.id

    # Round-trip first, so the audit records what was actually stored
    # ("22.50") rather than what was typed ("22.5").
    db.flush()
    db.refresh(row)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="StoreSettings", entity_id=row.id, user=admin,
        before=before, after=audit.snapshot(row, SETTINGS_AUDIT_FIELDS), request=request,
    )
    db.commit()
    db.refresh(row)
    return SettingsOut.model_validate(row)


@router.get("/store", response_model=StoreOut)
def read_store(user: CurrentUser, db: DbSession) -> StoreOut:
    store = db.get(Store, user.store_id)
    if store is None:
        raise NotFoundError("Store not found.")
    return StoreOut.model_validate(store)


@router.patch("/store", response_model=StoreOut)
def update_store(
    payload: StoreUpdate, request: Request, admin: AdminUser, db: DbSession
) -> StoreOut:
    store = db.get(Store, admin.store_id)
    if store is None:
        raise NotFoundError("Store not found.")

    before = audit.snapshot(store)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(store, field, value)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Store", entity_id=store.id, user=admin,
        before=before, after=audit.snapshot(store), request=request,
    )
    db.commit()
    db.refresh(store)
    return StoreOut.model_validate(store)
