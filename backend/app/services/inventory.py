"""Stock movements and the prices derived from them.

Two responsibilities:

* every change to `batches.qty_available_units` goes through `post_movement`,
  which writes the matching `stock_ledger` row in the same transaction;
* the price a customer would actually pay comes from the batch that would be
  sold next under FEFO — not from an average, and not from the master.
"""

from uuid import UUID, uuid4

from datetime import date, datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.db.types import quantize_qty, quantize_unit_price
from app.models.catalog import Medicine
from app.models.enums import NO_EXPIRY, ExpiryBasis, ItemType, MovementType, ReferenceType
from app.models.inventory import Batch, StockLedger
from app.models.store import StoreSettings
from app.utils.clock import business_date

ZERO = Decimal("0.000")


class StockError(AppError):
    code = "STOCK_INVALID"


def resolve_batch_no(batch_no: str | None, *, medicine: Medicine) -> str:
    """A medicine's batch is a traceability record and stays mandatory. A
    general item — soap, a thermometer — often has no batch the shop
    tracks at all, so leaving it blank is allowed there, and only there:
    a placeholder is generated instead of forcing the operator to invent
    one. `batches.batch_no` stays `NOT NULL` either way; what changes is
    only whether a person has to type something into it.
    """
    cleaned = (batch_no or "").strip()
    if cleaned:
        return cleaned
    if medicine.item_type == ItemType.MEDICINE.value:
        raise StockError(
            "batch_no is required for a medicine.",
            code="VALIDATION_ERROR",
            field="batch_no",
        )
    return f"GEN-{business_date().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"


def resolve_expiry(
    *,
    basis: str | None,
    expiry_date: date | None,
    mfg_date: date | None,
    best_before_months: int | None,
) -> tuple[date, str, date | None, int | None]:
    """Turn whatever the pack printed into one shelf date.

    Three things can be printed on a carton, and the counter should be able to
    key in whichever it is looking at:

    * an expiry date — the medicine case, typed as-is;
    * a manufacturing date and "best before 24 months" — the shampoo case,
      where the shelf date is arithmetic the operator should not have to do;
    * neither, because a comb does not go off.

    All three come back as `(expiry_date, basis, mfg_date, best_before_months)`
    ready to assign to a Batch, so that everything downstream — FEFO, the
    near-expiry alert, the expiry register — keeps working off a single
    non-null column. Stock with no shelf life gets `NO_EXPIRY`, which those
    queries treat as "not expiring", correctly and without knowing why.
    """
    resolved = ExpiryBasis(basis) if basis else None

    # No basis given: infer it from what was filled in, so an existing client
    # that only knows about expiry dates keeps working unchanged.
    if resolved is None:
        if expiry_date is not None:
            resolved = ExpiryBasis.EXPIRY_DATE
        elif mfg_date is not None and best_before_months is not None:
            resolved = ExpiryBasis.BEST_BEFORE
        else:
            raise StockError(
                "Give an expiry date, or a manufacturing date with a best-before "
                "period, or mark the item as one that does not expire.",
                code="EXPIRY_REQUIRED",
                field="expiry_date",
            )

    if resolved is ExpiryBasis.NONE:
        return NO_EXPIRY, resolved.value, None, None

    if resolved is ExpiryBasis.EXPIRY_DATE:
        if expiry_date is None:
            raise StockError(
                "An expiry date is required.", code="EXPIRY_REQUIRED", field="expiry_date"
            )
        if expiry_date >= NO_EXPIRY:
            raise StockError(
                "That expiry date is reserved for stock that never expires.",
                code="EXPIRY_INVALID",
                field="expiry_date",
            )
        return expiry_date, resolved.value, None, None

    # BEST_BEFORE — the shelf date is mfg + n months.
    if mfg_date is None or best_before_months is None:
        raise StockError(
            "A best-before period needs both a manufacturing date and a number "
            "of months.",
            code="EXPIRY_REQUIRED",
            field="mfg_date" if mfg_date is None else "best_before_months",
        )
    if best_before_months < 1:
        raise StockError(
            "Best before must be at least one month.",
            code="EXPIRY_INVALID",
            field="best_before_months",
        )
    if mfg_date > business_date():
        raise StockError(
            "A manufacturing date in the future is a keying error.",
            code="EXPIRY_INVALID",
            field="mfg_date",
        )

    computed = mfg_date + relativedelta(months=best_before_months)
    if computed >= NO_EXPIRY:
        raise StockError(
            "That best-before period runs past any date stock can carry.",
            code="EXPIRY_INVALID",
            field="best_before_months",
        )
    return computed, resolved.value, mfg_date, best_before_months


def unit_price_from_pack(pack_price: Decimal | None, units_per_pack: int) -> Decimal | None:
    """Price of one sellable unit — a tablet, or the whole bottle."""
    if pack_price is None:
        return None
    return quantize_unit_price(Decimal(pack_price) / Decimal(units_per_pack or 1))


def post_movement(
    db: Session,
    *,
    batch: Batch,
    movement_type: MovementType,
    qty_units_delta: Decimal,
    actor_id: UUID | None = None,
    reference_type: ReferenceType | None = None,
    reference_id: UUID | None = None,
    reason: str | None = None,
    allow_negative: bool = False,
) -> StockLedger:
    """Move stock and record it. The only sanctioned way to change a batch's
    available quantity (CLAUDE.md rule 3)."""
    delta = quantize_qty(qty_units_delta)
    if delta == ZERO:
        raise StockError("A stock movement cannot be zero.", field="qty_units")

    new_balance = quantize_qty(batch.qty_available_units + delta)
    if new_balance < ZERO and not allow_negative:
        raise StockError(
            f"Only {batch.qty_available_units} unit(s) left in batch {batch.batch_no}.",
            code="INSUFFICIENT_STOCK",
            field="qty_units",
        )

    batch.qty_available_units = new_balance
    if delta > ZERO:
        batch.qty_received_units = quantize_qty(batch.qty_received_units + delta)

    entry = StockLedger(
        store_id=batch.store_id,
        medicine_id=batch.medicine_id,
        batch_id=batch.id,
        movement_type=movement_type.value,
        qty_units_delta=delta,
        balance_after_units=new_balance,
        reference_type=reference_type.value if reference_type else None,
        reference_id=reference_id,
        reason=reason,
        created_by_id=actor_id,
        updated_by_id=actor_id,
    )
    db.add(entry)
    return entry


def is_sellable(batch: Batch, today: date | None = None) -> bool:
    """Expired or quarantined stock can never be offered — no override."""
    today = today or business_date()
    return (
        not batch.is_blocked
        and batch.expiry_date >= today
        and batch.qty_available_units > ZERO
    )


def sellable_batches(db: Session, store_id: UUID, medicine_ids: list[UUID]) -> dict[UUID, list[Batch]]:
    """Sellable batches per medicine, FEFO-ordered (earliest expiry first)."""
    if not medicine_ids:
        return {}

    today = business_date()
    rows = (
        db.query(Batch)
        .filter(
            Batch.store_id == store_id,
            Batch.medicine_id.in_(medicine_ids),
            Batch.is_blocked.is_(False),
            Batch.expiry_date >= today,
        )
        .order_by(Batch.expiry_date, Batch.id)
        .all()
    )

    grouped: dict[UUID, list[Batch]] = {}
    for batch in rows:
        if batch.qty_available_units > ZERO:
            grouped.setdefault(batch.medicine_id, []).append(batch)
    return grouped


def effective_discounts(
    batch: Batch | None, settings: StoreSettings
) -> tuple[Decimal, Decimal]:
    """(suggested, ceiling) percentages in force for a batch.

    A NULL on the batch falls back to the store policy. The suggested figure
    is clamped to the ceiling, so a store default of 10 % is never offered on
    a lot capped at 5 %.
    """
    ceiling = settings.max_discount_pct
    suggested = settings.default_discount_pct
    if batch is not None:
        if batch.max_discount_pct is not None:
            ceiling = batch.max_discount_pct
        if batch.default_discount_pct is not None:
            suggested = batch.default_discount_pct
    return min(suggested, ceiling), ceiling


class MedicineStock:
    """What the counter needs to know about one medicine right now."""

    __slots__ = (
        "available_units", "batch_count", "nearest_expiry", "current_batch",
        "mrp_per_pack", "unit_mrp", "gst_rate",
        "effective_default_discount_pct", "effective_max_discount_pct",
        "has_near_expiry",
    )

    def __init__(self, settings: StoreSettings):
        self.available_units = ZERO
        self.batch_count = 0
        self.nearest_expiry: date | None = None
        self.current_batch: Batch | None = None
        self.mrp_per_pack: Decimal | None = None
        self.unit_mrp: Decimal | None = None
        self.gst_rate: Decimal | None = None
        self.has_near_expiry = False
        self.effective_default_discount_pct, self.effective_max_discount_pct = (
            effective_discounts(None, settings)
        )


def stock_snapshot(
    db: Session, store_id: UUID, medicine_ids: list[UUID], settings: StoreSettings
) -> dict[UUID, MedicineStock]:
    """Per-medicine availability and the price that would actually be charged.

    Price comes from the FEFO batch — the one the POS would allocate next —
    so the catalogue never advertises a price the customer cannot get.
    """
    grouped = sellable_batches(db, store_id, medicine_ids)
    near_expiry_cutoff = _add_days(
        business_date(), settings.near_expiry_days
    )

    snapshot: dict[UUID, MedicineStock] = {}
    for medicine_id in medicine_ids:
        state = MedicineStock(settings)
        batches = grouped.get(medicine_id, [])
        if batches:
            first = batches[0]
            state.current_batch = first
            state.batch_count = len(batches)
            state.available_units = quantize_qty(
                sum((b.qty_available_units for b in batches), ZERO)
            )
            # The FEFO batch sorts first, so for stock that does expire it is
            # also the nearest expiry. Never advertise the sentinel date that
            # non-expiring stock carries — there is no expiry to report.
            state.nearest_expiry = first.expiry_date if first.has_expiry else None
            state.has_near_expiry = first.has_expiry and first.expiry_date <= near_expiry_cutoff
            state.mrp_per_pack = first.mrp_per_pack
            state.unit_mrp = first.unit_mrp
            state.gst_rate = first.gst_rate
            (
                state.effective_default_discount_pct,
                state.effective_max_discount_pct,
            ) = effective_discounts(first, settings)
        snapshot[medicine_id] = state
    return snapshot


def _add_days(value: date, days: int) -> date:
    from datetime import timedelta

    return value + timedelta(days=days)


def create_batch(
    db: Session,
    *,
    store_id: UUID,
    medicine: Medicine,
    batch_no: str | None,
    expiry_date: date,
    expiry_basis: str = ExpiryBasis.EXPIRY_DATE.value,
    mfg_date: date | None = None,
    best_before_months: int | None = None,
    qty_units: Decimal,
    mrp_per_pack: Decimal,
    cost_per_pack: Decimal | None = None,
    gst_rate: Decimal = Decimal("12.00"),
    default_discount_pct: Decimal | None = None,
    max_discount_pct: Decimal | None = None,
    received_date: date | None = None,
    movement_type: MovementType = MovementType.OPENING,
    actor_id: UUID | None = None,
    reference_type: ReferenceType = ReferenceType.ADJUSTMENT,
    reference_id: UUID | None = None,
    reason: str = "Opening stock",
) -> Batch:
    """Receive a consignment: creates the batch and its opening ledger row.

    The reference defaults to a manual adjustment; goods inward passes the
    purchase line instead, so every unit traces back to a supplier invoice.
    """
    if (
        default_discount_pct is not None
        and max_discount_pct is not None
        and default_discount_pct > max_discount_pct
    ):
        raise StockError(
            "Default discount cannot exceed the maximum discount.",
            code="DISCOUNT_POLICY_INVALID",
            field="default_discount_pct",
        )

    batch = Batch(
        store_id=store_id,
        medicine_id=medicine.id,
        batch_no=resolve_batch_no(batch_no, medicine=medicine),
        expiry_date=expiry_date,
        expiry_basis=expiry_basis,
        mfg_date=mfg_date,
        best_before_months=best_before_months,
        # Snapshot the pack: editing the master later must not rewrite what
        # physically arrived.
        pack_unit=medicine.pack_unit,
        pack_size=medicine.pack_size,
        units_per_pack=medicine.units_per_pack,
        qty_received_units=ZERO,
        qty_available_units=ZERO,
        mrp_per_pack=mrp_per_pack,
        unit_mrp=unit_price_from_pack(mrp_per_pack, medicine.units_per_pack),
        cost_per_pack=cost_per_pack,
        unit_cost=unit_price_from_pack(cost_per_pack, medicine.units_per_pack),
        gst_rate=gst_rate,
        default_discount_pct=default_discount_pct,
        max_discount_pct=max_discount_pct,
        received_date=received_date or business_date(),
        created_by_id=actor_id,
        updated_by_id=actor_id,
    )
    db.add(batch)
    db.flush()

    post_movement(
        db,
        batch=batch,
        movement_type=movement_type,
        qty_units_delta=qty_units,
        actor_id=actor_id,
        reference_type=reference_type,
        reference_id=reference_id,
        reason=reason,
    )
    # Order-book baseline: stock right after this consignment (Phase 6).
    medicine.stock_baseline_units = quantize_qty(
        (medicine.stock_baseline_units or ZERO) + quantize_qty(qty_units)
    )
    return batch
