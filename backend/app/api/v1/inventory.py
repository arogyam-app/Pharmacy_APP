"""Inventory: batches, stock on hand, and the ledger.

MRP, GST rate and discount limits are set here rather than on the medicine
master, because they change with each consignment.

Cost price is served only to admins, and by returning a different response
*model* — not by blanking a field — so a sales executive's token can never
surface it whatever the client asks for.
"""

from uuid import UUID

from decimal import Decimal

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.core.deps import require_permission, CurrentSettings, CurrentUser, DbSession, get_store_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.db.types import quantize_money, quantize_qty
from app.models.catalog import Composition, Medicine
from app.models.enums import (
    COUNTABLE_UNITS,
    AuditAction,
    ItemType,
    MovementType,
    ReferenceType,
)
from app.models.user import User
from app.models.inventory import Batch, StockLedger
from app.schemas.catalog import DeleteResult
from app.schemas.common import Page, PageParams
from app.schemas.inventory import (
    BatchAdmin,
    BatchCreate,
    BatchPublic,
    BatchUpdate,
    MedicineStockRow,
    StockAdjustment,
    StockLedgerEntry,
)
from app.services import audit, inventory
from app.utils.clock import business_date, utc_iso

#: Admin *and* still holding Inventory. Batch reads answer to the counter
#: too (see api/v1/router.py); creating and adjusting stock does not.
#: The counter sees stock; changing it is the owner's, and cost never
#: reaches a non-admin whatever is granted (CLAUDE.md rule 4).
StockKeeper = Annotated[User, Depends(require_permission("inventory:adjust"))]

#: Writing a batch — creating one, correcting its terms, moving its quantity.
#: Receiving goods *is* creating a batch, so somebody trusted with the goods
#: inward door reaches the same three endpoints from the Update stock panel;
#: they simply get the cost-free response model, as rule 4 requires.
StockWriter = Annotated[
    User, Depends(require_permission("inventory:adjust", "purchases:receive"))
]

router = APIRouter()

# Movements that mean a financial document points at the batch. Once any of
# these exist the batch can only be adjusted, never deleted.
PROTECTED_MOVEMENTS = (
    MovementType.SALE,
    MovementType.SALE_RETURN,
    MovementType.PURCHASE_RETURN,
    MovementType.VOID_REVERSAL,
)

AUDIT_FIELDS = [
    "id", "medicine_id", "batch_no", "expiry_date", "expiry_basis", "mfg_date",
    "best_before_months", "qty_received_units",
    "qty_available_units", "mrp_per_pack", "cost_per_pack", "gst_rate",
    "default_discount_pct", "max_discount_pct", "is_blocked",
]


def _serialise(batch: Batch, settings, *, as_admin: bool, medicine_name: str | None = None):
    today = business_date()
    suggested, ceiling = inventory.effective_discounts(batch, settings)
    packs = (
        quantize_qty(batch.qty_available_units / Decimal(batch.units_per_pack or 1))
    )
    # Stock that never expires has neither an expiry to show nor a countdown
    # to run: the stored date is a sentinel, not a fact about the goods.
    has_expiry = batch.has_expiry

    common = {
        "id": batch.id,
        "medicine_id": batch.medicine_id,
        "medicine_name": medicine_name,
        "batch_no": batch.batch_no,
        "expiry_date": batch.expiry_date,
        "expiry_basis": batch.expiry_basis,
        "has_expiry": has_expiry,
        "mfg_date": batch.mfg_date,
        "best_before_months": batch.best_before_months,
        "pack_unit": batch.pack_unit,
        "pack_size": batch.pack_size,
        "units_per_pack": batch.units_per_pack,
        "qty_received_units": batch.qty_received_units,
        "qty_available_units": batch.qty_available_units,
        "qty_available_packs": packs,
        "mrp_per_pack": batch.mrp_per_pack,
        "unit_mrp": batch.unit_mrp,
        "gst_rate": batch.gst_rate,
        "default_discount_pct": batch.default_discount_pct,
        "max_discount_pct": batch.max_discount_pct,
        "effective_default_discount_pct": suggested,
        "effective_max_discount_pct": ceiling,
        "received_date": batch.received_date,
        "is_blocked": batch.is_blocked,
        "block_reason": batch.block_reason,
        "is_expired": has_expiry and batch.expiry_date < today,
        "days_to_expiry": (batch.expiry_date - today).days if has_expiry else None,
    }
    if not as_admin:
        return BatchPublic(**common)

    margin = None
    if batch.cost_per_pack and batch.cost_per_pack > 0:
        margin = quantize_money(
            (batch.mrp_per_pack - batch.cost_per_pack) / batch.cost_per_pack * Decimal(100)
        )
    return BatchAdmin(
        **common,
        cost_per_pack=batch.cost_per_pack,
        unit_cost=batch.unit_cost,
        margin_pct=margin,
    )


# --- stock on hand -------------------------------------------------------


@router.get("/stock", response_model=Page[MedicineStockRow])
def stock_on_hand(
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    low_stock_only: bool = False,
    in_stock_only: bool = False,
    stocked_only: bool = False,
    item_type: ItemType | None = None,
) -> Page[MedicineStockRow]:
    query = (
        db.query(Medicine)
        .options(joinedload(Medicine.manufacturer), joinedload(Medicine.composition))
        .filter(Medicine.store_id == user.store_id, Medicine.is_active.is_(True))
    )
    if q:
        query = query.filter(Medicine.search_text.like(f"%{q.strip().lower()}%"))
    if item_type is not None:
        query = query.filter(Medicine.item_type == item_type.value)
    if stocked_only:
        # Ever purchased — any batch on file, including one now empty or
        # expired. `in_stock_only` asks the narrower question (units on the
        # shelf today); this one asks whether the medicine has a history to
        # update at all.
        query = query.filter(
            Medicine.id.in_(
                db.query(Batch.medicine_id).filter(Batch.store_id == user.store_id)
            )
        )

    medicines = query.order_by(Medicine.brand_name).all()
    snapshot = inventory.stock_snapshot(
        db, user.store_id, [m.id for m in medicines], settings
    )

    rows: list[MedicineStockRow] = []
    for medicine in medicines:
        state = snapshot[medicine.id]
        threshold = (
            medicine.reorder_level_units
            if medicine.reorder_level_units is not None
            else settings.low_stock_units
        )
        is_low = state.available_units <= threshold
        if low_stock_only and not is_low:
            continue
        if in_stock_only and state.available_units <= 0:
            continue

        rows.append(
            MedicineStockRow(
                medicine_id=medicine.id,
                brand_name=medicine.brand_name,
                composition_text=(
                    medicine.composition.composition_text if medicine.composition else None
                ),
                manufacturer_name=medicine.manufacturer.name,
                item_type=medicine.item_type,
                pack_label=medicine.pack_label,
                pack_unit=medicine.pack_unit,
                units_per_pack=medicine.units_per_pack,
                is_countable=medicine.pack_unit in {u.value for u in COUNTABLE_UNITS},
                available_units=state.available_units,
                available_packs=quantize_qty(
                    state.available_units / Decimal(medicine.units_per_pack or 1)
                ),
                batch_count=state.batch_count,
                nearest_expiry=state.nearest_expiry,
                has_near_expiry=state.has_near_expiry,
                mrp_per_pack=state.mrp_per_pack,
                unit_mrp=state.unit_mrp,
                gst_rate=state.gst_rate,
                reorder_level_units=medicine.reorder_level_units,
                is_low_stock=is_low,
            )
        )

    total = len(rows)
    window = rows[params.offset : params.offset + params.page_size]
    return Page.build(window, total, params)


# --- batches -------------------------------------------------------------


@router.get("/batches", response_model=Page[BatchAdmin | BatchPublic])
def list_batches(
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
    params: PageParams = Depends(),
    medicine_id: UUID | None = None,
    include_expired: bool = False,
    include_empty: bool = False,
):
    query = db.query(Batch).filter(Batch.store_id == user.store_id)
    if medicine_id is not None:
        query = query.filter(Batch.medicine_id == medicine_id)
    if not include_expired:
        query = query.filter(Batch.expiry_date >= business_date())
    if not include_empty:
        query = query.filter(Batch.qty_available_units > Decimal("0"))

    total = query.count()
    rows = (
        query.order_by(Batch.expiry_date, Batch.id)  # FEFO
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )

    names = dict(
        db.query(Medicine.id, Medicine.brand_name)
        .filter(Medicine.id.in_([b.medicine_id for b in rows]))
        .all()
    )
    items = [
        _serialise(b, settings, as_admin=user.is_admin, medicine_name=names.get(b.medicine_id))
        for b in rows
    ]
    return Page.build(items, total, params)


@router.post(
    "/batches", response_model=BatchAdmin | BatchPublic, status_code=status.HTTP_201_CREATED
)
def create_batch(payload: BatchCreate, request: Request, admin: StockWriter, db: DbSession):
    settings = get_store_settings(db, admin.store_id)
    medicine = db.get(Medicine, payload.medicine_id)
    if medicine is None or medicine.store_id != admin.store_id:
        raise NotFoundError("Medicine not found.", field="medicine_id")

    # One shelf date, however the carton stated it (printed expiry, mfg plus
    # best-before, or nothing at all).
    expiry_date, expiry_basis, mfg_date, best_before_months = inventory.resolve_expiry(
        basis=payload.expiry_basis,
        expiry_date=payload.expiry_date,
        mfg_date=payload.mfg_date,
        best_before_months=payload.best_before_months,
    )

    # Resolved once, up front: a blank batch_no is only ever allowed for a
    # general item, and the placeholder generated for it must be what the
    # clash check and the batch itself both see.
    resolved_batch_no = inventory.resolve_batch_no(payload.batch_no, medicine=medicine)

    clash = (
        db.query(Batch)
        .filter(
            Batch.store_id == admin.store_id,
            Batch.medicine_id == medicine.id,
            Batch.batch_no == resolved_batch_no,
            Batch.expiry_date == expiry_date,
        )
        .count()
    )
    if clash:
        raise ConflictError(
            "That batch number and expiry already exists for this medicine.",
            code="BATCH_EXISTS",
            field="batch_no",
        )

    qty_units = quantize_qty(payload.qty_packs * Decimal(medicine.units_per_pack or 1))
    batch = inventory.create_batch(
        db,
        store_id=admin.store_id,
        medicine=medicine,
        batch_no=resolved_batch_no,
        expiry_date=expiry_date,
        expiry_basis=expiry_basis,
        mfg_date=mfg_date,
        best_before_months=best_before_months,
        qty_units=qty_units,
        mrp_per_pack=payload.mrp_per_pack,
        cost_per_pack=payload.cost_per_pack,
        gst_rate=payload.gst_rate,
        default_discount_pct=payload.default_discount_pct,
        max_discount_pct=payload.max_discount_pct,
        received_date=payload.received_date,
        movement_type=MovementType.OPENING,
        actor_id=admin.id,
    )

    audit.record(
        db, action=AuditAction.CREATE, entity_type="Batch", entity_id=batch.id, user=admin,
        after=audit.snapshot(batch, AUDIT_FIELDS), request=request,
    )
    db.commit()
    db.refresh(batch)
    return _serialise(
        batch, settings, as_admin=admin.is_admin, medicine_name=medicine.brand_name
    )


@router.patch("/batches/{batch_id}", response_model=BatchAdmin | BatchPublic)
def update_batch(
    batch_id: UUID, payload: BatchUpdate, request: Request, admin: StockWriter, db: DbSession
):
    settings = get_store_settings(db, admin.store_id)
    batch = db.get(Batch, batch_id)
    if batch is None or batch.store_id != admin.store_id:
        raise NotFoundError("Batch not found.")

    before = audit.snapshot(batch, AUDIT_FIELDS)
    changes = payload.model_dump(exclude_unset=True)

    # Shelf life is re-resolved as a whole rather than field by field: a batch
    # switching from a printed expiry to a best-before period has to drop the
    # old date, and one switching to "never expires" has to drop both.
    EXPIRY_FIELDS = {"expiry_basis", "expiry_date", "mfg_date", "best_before_months"}
    if EXPIRY_FIELDS & changes.keys():
        stated = {field: changes.pop(field, None) for field in EXPIRY_FIELDS}
        # Unstated parts keep what the batch already holds, so a lone
        # expiry_date correction on an ordinary batch still works.
        basis = stated["expiry_basis"] or batch.expiry_basis
        (
            batch.expiry_date,
            batch.expiry_basis,
            batch.mfg_date,
            batch.best_before_months,
        ) = inventory.resolve_expiry(
            basis=basis,
            expiry_date=stated["expiry_date"] or batch.expiry_date,
            mfg_date=stated["mfg_date"] or batch.mfg_date,
            best_before_months=stated["best_before_months"] or batch.best_before_months,
        )

    for field, value in changes.items():
        setattr(batch, field, value)

    # Keep the derived per-unit figures in step with the pack prices.
    if "mrp_per_pack" in changes:
        batch.unit_mrp = inventory.unit_price_from_pack(batch.mrp_per_pack, batch.units_per_pack)
    if "cost_per_pack" in changes:
        batch.unit_cost = inventory.unit_price_from_pack(batch.cost_per_pack, batch.units_per_pack)

    if (
        batch.default_discount_pct is not None
        and batch.max_discount_pct is not None
        and batch.default_discount_pct > batch.max_discount_pct
    ):
        raise inventory.StockError(
            "Default discount cannot exceed the maximum discount.",
            code="DISCOUNT_POLICY_INVALID",
            field="default_discount_pct",
        )

    batch.updated_by_id = admin.id
    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Batch", entity_id=batch.id, user=admin,
        before=before, after=audit.snapshot(batch, AUDIT_FIELDS), request=request,
    )
    db.commit()
    db.refresh(batch)

    medicine = db.get(Medicine, batch.medicine_id)
    return _serialise(
        batch, settings, as_admin=admin.is_admin, medicine_name=medicine.brand_name
    )


@router.post("/batches/{batch_id}/adjust", response_model=BatchAdmin | BatchPublic)
def adjust_batch_quantity(
    batch_id: UUID, payload: StockAdjustment, request: Request, admin: StockWriter, db: DbSession
):
    """Correct a batch quantity through the ledger.

    Quantities are never edited in place: a physical-count correction, a
    breakage or a keying error each becomes its own signed movement, so the
    ledger keeps explaining how the current figure was reached.
    """
    settings = get_store_settings(db, admin.store_id)
    batch = db.get(Batch, batch_id)
    if batch is None or batch.store_id != admin.store_id:
        raise NotFoundError("Batch not found.")

    delta = payload.qty_units_delta
    movement = MovementType.ADJUSTMENT_IN if delta > 0 else MovementType.ADJUSTMENT_OUT
    before = audit.snapshot(batch, AUDIT_FIELDS)

    inventory.post_movement(
        db,
        batch=batch,
        movement_type=movement,
        qty_units_delta=delta,
        actor_id=admin.id,
        reference_type=ReferenceType.ADJUSTMENT,
        reason=payload.reason,
    )

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Batch", entity_id=batch.id, user=admin,
        before=before, after=audit.snapshot(batch, AUDIT_FIELDS), reason=payload.reason,
        request=request,
    )
    db.commit()
    db.refresh(batch)

    medicine = db.get(Medicine, batch.medicine_id)
    return _serialise(
        batch, settings, as_admin=admin.is_admin, medicine_name=medicine.brand_name
    )


@router.delete("/batches/{batch_id}", response_model=DeleteResult)
def delete_batch(
    batch_id: UUID, request: Request, admin: StockKeeper, db: DbSession
) -> DeleteResult:
    """Removes a batch entered in error, along with its own ledger rows.

    Only possible while nothing has been sold or returned against it. Once a
    financial document points at the batch, deleting would falsify that
    document, so the correct remedy is a stock adjustment instead. The
    audit log keeps a copy of whatever is removed.
    """
    batch = db.get(Batch, batch_id)
    if batch is None or batch.store_id != admin.store_id:
        raise NotFoundError("Batch not found.")

    protected = (
        db.query(func.count(StockLedger.id))
        .filter(
            StockLedger.batch_id == batch.id,
            StockLedger.movement_type.in_([m.value for m in PROTECTED_MOVEMENTS]),
        )
        .scalar()
    )
    if protected:
        raise ConflictError(
            f"Batch {batch.batch_no} has {protected} sale or return movement(s) against it. "
            f"Correct the quantity with an adjustment instead — deleting it would "
            f"falsify those documents.",
            code="BATCH_HAS_MOVEMENTS",
        )

    before = audit.snapshot(batch, AUDIT_FIELDS)
    medicine = db.get(Medicine, batch.medicine_id)
    if medicine is not None:
        # Undo the order-book baseline this receipt contributed (Phase 6).
        medicine.stock_baseline_units = max(
            Decimal("0.000"),
            quantize_qty(medicine.stock_baseline_units - batch.qty_received_units),
        )

    db.query(StockLedger).filter(StockLedger.batch_id == batch.id).delete(
        synchronize_session=False
    )
    db.delete(batch)

    audit.record(
        db, action=AuditAction.DELETE, entity_type="Batch", entity_id=batch_id, user=admin,
        before=before, reason="Batch removed before any sale.", request=request,
    )
    db.commit()
    return DeleteResult(deleted=True, id=batch_id, message="Batch deleted.")


@router.get("/ledger", response_model=Page[StockLedgerEntry])
def stock_ledger(
    admin: StockKeeper,
    db: DbSession,
    params: PageParams = Depends(),
    batch_id: UUID | None = None,
    medicine_id: UUID | None = None,
) -> Page[StockLedgerEntry]:
    query = db.query(StockLedger).filter(StockLedger.store_id == admin.store_id)
    if batch_id is not None:
        query = query.filter(StockLedger.batch_id == batch_id)
    if medicine_id is not None:
        query = query.filter(StockLedger.medicine_id == medicine_id)

    total = query.count()
    rows = (
        query.order_by(StockLedger.id.desc())
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    return Page.build(
        [
            StockLedgerEntry(
                id=r.id,
                batch_id=r.batch_id,
                medicine_id=r.medicine_id,
                movement_type=r.movement_type,
                qty_units_delta=r.qty_units_delta,
                balance_after_units=r.balance_after_units,
                reference_type=r.reference_type,
                reference_id=r.reference_id,
                reason=r.reason,
                created_at=utc_iso(r.created_at),
            )
            for r in rows
        ],
        total,
        params,
    )
