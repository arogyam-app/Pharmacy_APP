"""Alerts, the expiry register and the order book.

Alerts are open to the counter — a sales executive needs to know what is
about to expire and what has run out.

The expiry register and the order book default to the owner, because both
are about money leaving the business, but either can be handed to a trusted
executive through the module toggles on the Users screen. When it is, the
work is delegated and the money is not: the responses drop to the cost-free
models, so the person sees *what* expired or *what* to reorder without ever
being told what the shop paid for it (CLAUDE.md rule 4).
"""

from uuid import UUID

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from app.core.deps import CurrentSettings, CurrentUser, DbSession, require_permission
from app.core.exceptions import NotFoundError
from app.models.catalog import Medicine
from app.models.enums import AuditAction, ExpiryStatus, ItemType, OrderBookStatus
from app.models.party import Supplier
from app.models.user import User
from app.models.stockwatch import ExpiryRegisterEntry, OrderBookEntry
from app.schemas.common import Page, PageParams
from app.schemas.stockwatch import (
    AlertSummary,
    BatchAlertRow,
    BatchAlertRowAdmin,
    ExpiryEntryAdmin,
    ExpiryEntryOut,
    ExpiryScanResult,
    OrderBookRow,
    OrderBookRowAdmin,
    OrderBookUpdate,
    RecomputeResult,
    RecordCreditIn,
    ReturnToSupplierIn,
    StockAlertRow,
    WriteOffIn,
)
from app.services import alerts, audit, expiry, order_book
from app.utils.csv_export import csv_response
from app.utils.clock import business_date, utc_iso

#: Both registers can be handed to a trusted executive. The routes then
#: return the cost-free response models — an expired batch is a fact about
#: stock; what it cost the shop is not (CLAUDE.md rule 4).
#: Both registers can be handed to a trusted executive, and the counter
#: sees the expiry one by default. The responses drop to their cost-free
#: models for a non-admin — an expired batch is a fact about stock; what
#: it cost the shop is not (CLAUDE.md rule 4).
ExpiryReader = Annotated[
    User, Depends(require_permission("expiry:read", "expiry:manage"))
]
ExpiryKeeper = Annotated[User, Depends(require_permission("expiry:manage"))]
OrderBookReader = Annotated[
    User, Depends(require_permission("order_book:read", "order_book:manage"))
]
OrderBookKeeper = Annotated[User, Depends(require_permission("order_book:manage"))]

alerts_router = APIRouter()
expiry_router = APIRouter()
order_book_router = APIRouter()


# --- alerts --------------------------------------------------------------


#: Medicines and general goods are stocked, watched and re-ordered by
#: different people looking at different shelves, so every alert list can be
#: scoped to one of them. Omitted, it covers the whole shop — which is what
#: the dashboard wants.
ItemScope = Annotated[
    ItemType | None,
    Query(description="Scope to medicines or to general goods; omit for both"),
]


def _scope(item_type: ItemType | None) -> str | None:
    return item_type.value if item_type is not None else None


@alerts_router.get("/summary", response_model=AlertSummary)
def summary(
    user: CurrentUser, settings: CurrentSettings, db: DbSession, item_type: ItemScope = None
) -> AlertSummary:
    return AlertSummary(**alerts.summary(db, user.store_id, settings, _scope(item_type)))


@alerts_router.get("/expired", response_model=list[BatchAlertRowAdmin | BatchAlertRow])
def expired(user: CurrentUser, db: DbSession, item_type: ItemScope = None):
    """Past its date with stock still on hand — money already lost."""
    today = business_date()
    batches = alerts.expired_batches(db, user.store_id, _scope(item_type))
    return _batch_rows(db, batches, today, as_admin=user.is_admin)


@alerts_router.get("/expiring", response_model=list[BatchAlertRowAdmin | BatchAlertRow])
def expiring(
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
    days: int | None = Query(None, ge=1, le=730),
    item_type: ItemScope = None,
):
    """Still sellable — the window exists so that it gets sold."""
    today = business_date()
    window = days if days is not None else settings.near_expiry_days
    batches = alerts.expiring_batches(db, user.store_id, window, _scope(item_type))
    return _batch_rows(db, batches, today, as_admin=user.is_admin)


@alerts_router.get("/low-stock", response_model=list[StockAlertRow])
def low_stock(
    user: CurrentUser, settings: CurrentSettings, db: DbSession, item_type: ItemScope = None
):
    low, _ = alerts.stock_alerts(db, user.store_id, settings, _scope(item_type))
    return [StockAlertRow(**row) for row in low]


@alerts_router.get("/out-of-stock", response_model=list[StockAlertRow])
def out_of_stock(
    user: CurrentUser, settings: CurrentSettings, db: DbSession, item_type: ItemScope = None
):
    _, out = alerts.stock_alerts(db, user.store_id, settings, _scope(item_type))
    return [StockAlertRow(**row) for row in out]


def _batch_rows(db, batches, today, *, as_admin: bool):
    names = dict(
        db.query(Medicine.id, Medicine.brand_name).filter(
            Medicine.id.in_([b.medicine_id for b in batches])
        )
    )
    rows = [alerts.batch_row(b, names.get(b.medicine_id), today) for b in batches]
    if as_admin:
        return [BatchAlertRowAdmin(**row) for row in rows]
    return [BatchAlertRow(**{k: v for k, v in row.items() if k != "cost_value"}) for row in rows]


# --- expiry register -----------------------------------------------------


@expiry_router.post("/scan", response_model=ExpiryScanResult)
def scan(request: Request, user: ExpiryKeeper, db: DbSession) -> ExpiryScanResult:
    """Pull expired batches with stock into the register. Safe to re-run."""
    result = expiry.scan(db, store_id=user.store_id, actor_id=user.id)
    if result["added"] or result["updated"]:
        audit.record(
            db, action=AuditAction.CREATE, entity_type="ExpiryRegister", user=user,
            after=result, request=request,
        )
    db.commit()
    return ExpiryScanResult(
        **result,
        message=(
            f"{result['added']} new entr(ies), {result['updated']} updated, "
            f"from {result['scanned']} expired batch(es)."
        ),
    )


@expiry_router.get("", response_model=Page[ExpiryEntryAdmin | ExpiryEntryOut])
def list_expiry(
    user: ExpiryReader,
    db: DbSession,
    params: PageParams = Query(default_factory=PageParams),
    status_filter: ExpiryStatus | None = Query(None, alias="status"),
    item_type: ItemScope = None,
) -> Page[ExpiryEntryAdmin | ExpiryEntryOut]:
    query = db.query(ExpiryRegisterEntry).filter(
        ExpiryRegisterEntry.store_id == user.store_id
    )
    if status_filter is not None:
        query = query.filter(ExpiryRegisterEntry.status == status_filter.value)
    if item_type is not None:
        query = query.join(
            Medicine, ExpiryRegisterEntry.medicine_id == Medicine.id
        ).filter(Medicine.item_type == item_type.value)

    total = query.count()
    rows = (
        query.order_by(ExpiryRegisterEntry.expiry_date, ExpiryRegisterEntry.id)
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    return Page.build([_expiry_out(db, r, user.is_admin) for r in rows], total, params)


@expiry_router.post("/{entry_id}/write-off", response_model=ExpiryEntryAdmin | ExpiryEntryOut)
def write_off(
    entry_id: UUID, payload: WriteOffIn, request: Request, user: ExpiryKeeper, db: DbSession
) -> ExpiryEntryAdmin | ExpiryEntryOut:
    """Book the loss and take the units off the shelf through the ledger."""
    entry = _load_expiry(db, entry_id, user.store_id)
    expiry.write_off(db, entry=entry, user=user, notes=payload.notes)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="ExpiryRegister", entity_id=entry.id,
        user=user,
        after={"batch_no": entry.batch_no, "status": entry.status,
               "qty_units": entry.qty_units, "cost_value": entry.cost_value},
        reason=payload.notes or "Expired stock written off", request=request,
    )
    db.commit()
    db.refresh(entry)
    return _expiry_out(db, entry, user.is_admin)


@expiry_router.post("/{entry_id}/return-to-supplier", response_model=ExpiryEntryAdmin | ExpiryEntryOut)
def return_to_supplier(
    entry_id: UUID,
    payload: ReturnToSupplierIn,
    request: Request,
    user: ExpiryKeeper,
    db: DbSession,
) -> ExpiryEntryAdmin | ExpiryEntryOut:
    """Send the goods back against a debit note, with credit expected."""
    entry = _load_expiry(db, entry_id, user.store_id)
    expiry.return_to_supplier(
        db,
        entry=entry,
        supplier_id=payload.supplier_id,
        debit_note_ref=payload.debit_note_ref,
        user=user,
        credit_expected=payload.credit_expected,
        notes=payload.notes,
    )

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="ExpiryRegister", entity_id=entry.id,
        user=user,
        after={"batch_no": entry.batch_no, "status": entry.status,
               "supplier_id": entry.supplier_id, "debit_note_ref": entry.debit_note_ref},
        reason=payload.notes or "Expired stock returned to supplier", request=request,
    )
    db.commit()
    db.refresh(entry)
    return _expiry_out(db, entry, user.is_admin)


@expiry_router.post("/{entry_id}/record-credit", response_model=ExpiryEntryAdmin | ExpiryEntryOut)
def record_credit(
    entry_id: UUID, payload: RecordCreditIn, request: Request, user: ExpiryKeeper, db: DbSession
) -> ExpiryEntryAdmin | ExpiryEntryOut:
    """The distributor's credit arrived — close the loop."""
    entry = _load_expiry(db, entry_id, user.store_id)
    expiry.record_credit(db, entry=entry, amount=payload.amount, user=user)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="ExpiryRegister", entity_id=entry.id,
        user=user,
        after={"batch_no": entry.batch_no, "status": entry.status,
               "credit_received_amount": entry.credit_received_amount},
        request=request,
    )
    db.commit()
    db.refresh(entry)
    return _expiry_out(db, entry, user.is_admin)


def _load_expiry(db, entry_id: UUID, store_id: UUID) -> ExpiryRegisterEntry:
    entry = db.get(ExpiryRegisterEntry, entry_id)
    if entry is None or entry.store_id != store_id:
        raise NotFoundError("Expiry register entry not found.")
    return entry


def _expiry_out(
    db, entry: ExpiryRegisterEntry, as_admin: bool = True
) -> ExpiryEntryAdmin | ExpiryEntryOut:
    """The register row, with the cost of the loss only for an admin.

    An executive granted the expiry register sees what expired, how much of
    it, and what was done about it. What the shop paid for it is a cost
    price, and those do not leave the API for a non-admin (rule 4).
    """
    supplier = db.get(Supplier, entry.supplier_id) if entry.supplier_id else None
    today = business_date()
    model = ExpiryEntryAdmin if as_admin else ExpiryEntryOut
    return model(
        id=entry.id,
        medicine_id=entry.medicine_id,
        medicine_name=entry.medicine_name_snapshot,
        batch_id=entry.batch_id,
        batch_no=entry.batch_no,
        expiry_date=entry.expiry_date,
        days_expired=(today - entry.expiry_date).days,
        qty_units=entry.qty_units,
        mrp_value=entry.mrp_value,
        **({"cost_value": entry.cost_value} if as_admin else {}),
        source=entry.source,
        status=entry.status,
        action_date=entry.action_date,
        actioned_by_name=entry.actioned_by_name,
        supplier_id=entry.supplier_id,
        supplier_name=supplier.name if supplier else None,
        debit_note_ref=entry.debit_note_ref,
        credit_received_amount=entry.credit_received_amount,
        notes=entry.notes,
    )


# --- order book ----------------------------------------------------------


@order_book_router.get("", response_model=Page[OrderBookRowAdmin | OrderBookRow])
def list_order_book(
    user: OrderBookReader,
    settings: CurrentSettings,
    db: DbSession,
    params: PageParams = Query(default_factory=PageParams),
    status_filter: OrderBookStatus | None = Query(OrderBookStatus.OPEN, alias="status"),
    supplier_id: UUID | None = None,
) -> Page[OrderBookRowAdmin | OrderBookRow]:
    """What to reorder. Grouped by supplier in the export."""
    query = db.query(OrderBookEntry).filter(OrderBookEntry.store_id == user.store_id)
    if status_filter is not None:
        query = query.filter(OrderBookEntry.status == status_filter.value)
    if supplier_id is not None:
        query = query.filter(OrderBookEntry.preferred_supplier_id == supplier_id)

    total = query.count()
    rows = (
        query.order_by(OrderBookEntry.preferred_supplier_id, OrderBookEntry.id)
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )
    return Page.build(_order_rows(db, rows, user.store_id, user.is_admin), total, params)


@order_book_router.post("/recompute", response_model=RecomputeResult)
def recompute(
    request: Request, user: OrderBookKeeper, settings: CurrentSettings, db: DbSession
) -> RecomputeResult:
    """Re-evaluate the whole catalogue against the decline rule."""
    result = order_book.recompute_all(
        db, store_id=user.store_id, settings=settings, actor_id=user.id
    )
    audit.record(
        db, action=AuditAction.UPDATE, entity_type="OrderBook", user=user,
        after=result, request=request,
    )
    db.commit()
    return RecomputeResult(
        **result,
        message=(
            f"{result['raised']} raised, {result['refreshed']} refreshed, "
            f"{result['closed']} closed."
        ),
    )


@order_book_router.patch("/{entry_id}", response_model=OrderBookRowAdmin | OrderBookRow)
def update_entry(
    entry_id: UUID, payload: OrderBookUpdate, request: Request, user: OrderBookKeeper, db: DbSession
) -> OrderBookRowAdmin | OrderBookRow:
    """Adjust the suggestion, pick a supplier, or mark it ordered."""
    entry = _load_order(db, entry_id, user.store_id)
    before = audit.snapshot(
        entry, ["suggested_qty_packs", "preferred_supplier_id", "status"]
    )

    changes = payload.model_dump(exclude_unset=True)
    if "suggested_qty_packs" in changes and changes["suggested_qty_packs"] is not None:
        medicine = db.get(Medicine, entry.medicine_id)
        per = medicine.units_per_pack if medicine else 1
        entry.suggested_qty_packs = changes["suggested_qty_packs"]
        entry.suggested_qty_units = changes["suggested_qty_packs"] * per
        changes.pop("suggested_qty_packs")
    if "status" in changes and changes["status"] is not None:
        new_status = str(changes.pop("status"))
        entry.status = new_status
        now = datetime.now(timezone.utc)
        if new_status == OrderBookStatus.ORDERED.value:
            entry.ordered_at = now
        elif new_status in (OrderBookStatus.RECEIVED.value, OrderBookStatus.CANCELLED.value):
            entry.closed_at = now

    for field, value in changes.items():
        setattr(entry, field, value)
    entry.updated_by_id = user.id

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="OrderBook", entity_id=entry.id,
        user=user, before=before,
        after=audit.snapshot(entry, ["suggested_qty_packs", "preferred_supplier_id", "status"]),
        request=request,
    )
    db.commit()
    db.refresh(entry)
    return _order_rows(db, [entry], user.store_id, user.is_admin)[0]


@order_book_router.get("/export")
def export_order_book(
    user: OrderBookReader,
    db: DbSession,
    status_filter: OrderBookStatus | None = Query(OrderBookStatus.OPEN, alias="status"),
) -> StreamingResponse:
    """CSV grouped by supplier — the list you hand to a distributor."""
    query = db.query(OrderBookEntry).filter(OrderBookEntry.store_id == user.store_id)
    if status_filter is not None:
        query = query.filter(OrderBookEntry.status == status_filter.value)
    rows = _order_rows(
        db,
        query.order_by(OrderBookEntry.preferred_supplier_id, OrderBookEntry.id).all(),
        user.store_id,
        user.is_admin,
    )

    money_columns = ["Last cost", "Est. value"] if user.is_admin else []

    def line(row) -> list:
        base = [
            row.preferred_supplier_name or "— no supplier —",
            row.medicine_name or "",
            row.pack_label or "",
            row.available_units_now if row.available_units_now is not None else "",
            row.suggested_qty_packs,
            row.suggested_qty_units,
            row.avg_monthly_consumption_units,
            row.days_of_cover if row.days_of_cover is not None else "",
        ]
        if user.is_admin:
            base += [
                row.last_purchase_cost if row.last_purchase_cost is not None else "",
                row.estimated_value if row.estimated_value is not None else "",
            ]
        return [*base, row.trigger_reason, row.status]

    return csv_response(
        filename=f"order-book-{business_date()}.csv",
        header=[
            "Supplier", "Medicine", "Pack", "Available units", "Suggested packs",
            "Suggested units", "Monthly use", "Days cover", *money_columns,
            "Trigger", "Status",
        ],
        rows=[line(row) for row in rows],
    )


def _load_order(db, entry_id: UUID, store_id: UUID) -> OrderBookEntry:
    entry = db.get(OrderBookEntry, entry_id)
    if entry is None or entry.store_id != store_id:
        raise NotFoundError("Order-book entry not found.")
    return entry


def _order_rows(
    db, entries, store_id: UUID, as_admin: bool = True
) -> list[OrderBookRowAdmin | OrderBookRow]:
    """What to reorder — with the last cost and the estimated bill only for
    an admin. An executive still sees *what* and *how many*, which is the
    part they act on; the money is the owner's (rule 4)."""
    if not entries:
        return []

    medicines = {
        m.id: m
        for m in db.query(Medicine).filter(
            Medicine.id.in_([e.medicine_id for e in entries])
        )
    }
    suppliers = dict(
        db.query(Supplier.id, Supplier.name).filter(
            Supplier.id.in_([e.preferred_supplier_id for e in entries if e.preferred_supplier_id])
        )
    )
    available = alerts.sellable_units_by_medicine(db, store_id)

    rows = []
    for entry in entries:
        medicine = medicines.get(entry.medicine_id)
        estimated = (
            entry.last_purchase_cost * entry.suggested_qty_packs
            if entry.last_purchase_cost is not None
            else None
        )
        model = OrderBookRowAdmin if as_admin else OrderBookRow
        rows.append(
            model(
                id=entry.id,
                medicine_id=entry.medicine_id,
                medicine_name=medicine.brand_name if medicine else None,
                pack_label=medicine.pack_label if medicine else None,
                units_per_pack=medicine.units_per_pack if medicine else None,
                trigger_reason=entry.trigger_reason,
                baseline_units=entry.baseline_units,
                available_units_at_trigger=entry.available_units_at_trigger,
                available_units_now=available.get(entry.medicine_id),
                decline_pct=entry.decline_pct,
                suggested_qty_units=entry.suggested_qty_units,
                suggested_qty_packs=entry.suggested_qty_packs,
                avg_monthly_consumption_units=entry.avg_monthly_consumption_units,
                days_of_cover=entry.days_of_cover,
                preferred_supplier_id=entry.preferred_supplier_id,
                preferred_supplier_name=suppliers.get(entry.preferred_supplier_id),
                status=entry.status,
                ordered_at=utc_iso(entry.ordered_at),
                purchase_invoice_id=entry.purchase_invoice_id,
                closed_at=utc_iso(entry.closed_at),
                notes=entry.notes,
                **(
                    {"last_purchase_cost": entry.last_purchase_cost,
                     "estimated_value": estimated}
                    if as_admin
                    else {}
                ),
            )
        )
    return rows
