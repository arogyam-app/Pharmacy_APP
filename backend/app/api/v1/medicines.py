"""Medicines: CRUD, the counter search box, alternatives, and bulk import.

Two endpoints carry the feature the shop actually asked for:

* ``GET /medicines/search`` — one box that matches a brand name *or* a
  generic/salt name, because the operator does not know which the customer
  will say.
* ``GET /medicines/{id}/alternatives`` — every other brand sharing the exact
  composition, cheapest first, so a substitute can be offered on the spot.
"""

from uuid import UUID

from decimal import Decimal, InvalidOperation

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, selectinload

from app.core.deps import require_permission, CurrentSettings, CurrentUser, DbSession, get_store_settings
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.models.catalog import Composition, CompositionSalt, Manufacturer, Medicine
from app.models.enums import (
    COUNTABLE_UNITS,
    AuditAction,
    DosageForm,
    ItemType,
    PackUnit,
    ScheduleType,
)
from app.models.user import User
from app.models.inventory import Batch
from app.models.store import StoreSettings
from app.schemas.catalog import (
    DeleteResult,
    FormHintOut,
    ImportResult,
    ImportRowError,
    MedicineCreate,
    MedicineMatch,
    MedicineMatchResult,
    MedicineOut,
    MedicineSearchResult,
    MedicineUpdate,
    PackHintOut,
)
from app.schemas.common import Page, PageParams
from app.services import audit, catalog, inventory, matching, packinfo
from app.utils import spreadsheet
from app.utils.spreadsheet import Column

#: Admin *and* still holding Masters. The read routes above answer to the
#: counter as well (see api/v1/router.py); maintaining the data does not.
#: Adding a brand mid-sale is the counter's job; changing one is not.
#: Pack size, HSN code and schedule drive the unit maths, the tax on the
#: bill and the prescription check, so editing stays with whoever holds
#: `medicines:edit`.
MedicineAuthor = Annotated[User, Depends(require_permission("medicines:create"))]
MedicineEditor = Annotated[User, Depends(require_permission("medicines:edit"))]
MedicineRemover = Annotated[User, Depends(require_permission("medicines:delete"))]

router = APIRouter()

AUDIT_FIELDS = [
    "id", "brand_name", "manufacturer_id", "item_type", "composition_id",
    "dosage_form", "pack_size",
    "pack_unit", "pack_label", "units_per_pack", "schedule_type", "hsn_code",
    "list_mrp_per_pack", "reorder_level_units", "rack_location", "is_active",
]


def _loaded(query):
    return query.options(
        joinedload(Medicine.manufacturer),
        joinedload(Medicine.composition).selectinload(Composition.components),
    )


def _is_countable(medicine: Medicine) -> bool:
    """Whether the pack holds articles a customer can buy one of.

    The pack *unit* decides it, not the dosage form: a general item may be a
    box of twelve soaps (countable) or a 500 ml bottle of cleaner (not).
    """
    return medicine.pack_unit in {u.value for u in COUNTABLE_UNITS}


def _to_out(
    medicine: Medicine, settings: StoreSettings, stock: inventory.MedicineStock
) -> MedicineOut:
    return MedicineOut(
        id=medicine.id,
        store_id=medicine.store_id,
        brand_name=medicine.brand_name,
        manufacturer_id=medicine.manufacturer_id,
        manufacturer_name=medicine.manufacturer.name,
        item_type=medicine.item_type,
        composition_id=medicine.composition_id,
        composition_text=(
            medicine.composition.composition_text if medicine.composition else None
        ),
        therapeutic_class=(
            medicine.composition.therapeutic_class if medicine.composition else None
        ),
        dosage_form=medicine.dosage_form,
        pack_size=medicine.pack_size,
        pack_unit=medicine.pack_unit,
        pack_label=medicine.pack_label,
        units_per_pack=medicine.units_per_pack,
        is_countable=_is_countable(medicine),
        schedule_type=medicine.schedule_type,
        is_prescription_required=medicine.is_prescription_required,
        hsn_code=medicine.hsn_code,
        list_mrp_per_pack=medicine.list_mrp_per_pack,
        reorder_level_units=medicine.reorder_level_units,
        rack_location=medicine.rack_location,
        is_active=medicine.is_active,
        available_units=stock.available_units,
        batch_count=stock.batch_count,
        nearest_expiry=stock.nearest_expiry,
        has_near_expiry=stock.has_near_expiry,
        mrp_per_pack=stock.mrp_per_pack,
        unit_mrp=stock.unit_mrp,
        gst_rate=stock.gst_rate,
        effective_default_discount_pct=stock.effective_default_discount_pct,
        effective_max_discount_pct=stock.effective_max_discount_pct,
    )


def to_search_result(
    medicine: Medicine,
    settings: StoreSettings,
    stock: inventory.MedicineStock,
    *,
    is_alternative: bool = False,
) -> MedicineSearchResult:
    """One catalogue row as the counter and the matcher both show it.

    Public because goods inward serialises match candidates with it too, and
    two copies of this shape would drift.

    `is_alternative` marks a row the search offered because it *shares a
    composition* with what was asked for, rather than because its own name
    matched — so the counter can say why it is on the list.
    """
    return MedicineSearchResult(
        is_alternative=is_alternative,
        id=medicine.id,
        brand_name=medicine.brand_name,
        item_type=medicine.item_type,
        composition_id=medicine.composition_id,
        composition_text=(
            medicine.composition.composition_text if medicine.composition else None
        ),
        manufacturer_name=medicine.manufacturer.name,
        dosage_form=medicine.dosage_form,
        pack_size=medicine.pack_size,
        pack_unit=medicine.pack_unit,
        pack_label=medicine.pack_label,
        units_per_pack=medicine.units_per_pack,
        is_countable=_is_countable(medicine),
        schedule_type=medicine.schedule_type,
        is_prescription_required=medicine.is_prescription_required,
        mrp_per_pack=stock.mrp_per_pack,
        unit_mrp=stock.unit_mrp,
        gst_rate=stock.gst_rate,
        effective_default_discount_pct=stock.effective_default_discount_pct,
        effective_max_discount_pct=stock.effective_max_discount_pct,
        available_units=stock.available_units,
        batch_count=stock.batch_count,
        nearest_expiry=stock.nearest_expiry,
        has_near_expiry=stock.has_near_expiry,
        in_stock=stock.available_units > 0,
    )


# --- search --------------------------------------------------------------


@router.get("/search", response_model=list[MedicineSearchResult])
def search_medicines(
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
    q: str = Query(min_length=2, description="Brand name or generic/salt name"),
    limit: int = Query(20, ge=1, le=50),
    include_inactive: bool = False,
    item_type: ItemType | None = Query(
        None, description="Scope to medicines or to general goods; omit for both"
    ),
) -> list[MedicineSearchResult]:
    needle = catalog.normalise_name(q).lower()

    # Everything the shop could offer, before the search text narrows it.
    # The substitute pass below reuses this: a brand sharing a composition is
    # an answer whether or not its own name matches, which is the whole point
    # of offering one.
    base = _loaded(db.query(Medicine)).filter(Medicine.store_id == user.store_id)
    if not include_inactive:
        base = base.filter(Medicine.is_active.is_(True))
    # The counter searches everything — a basket mixes both. The maintenance
    # screens each scope themselves to their own kind.
    if item_type is not None:
        base = base.filter(Medicine.item_type == item_type.value)

    query = base.filter(Medicine.search_text.like(f"%{needle}%"))

    # Brand-name matches first — the operator usually typed a brand — then
    # alphabetically so the list is stable between keystrokes. Fetched wider
    # than `limit` because what survives to the top depends on stock, which
    # the database cannot order by (availability is a ledger projection).
    named = (
        query.order_by(
            func.lower(Medicine.brand_name).like(f"{needle}%").desc(),
            Medicine.brand_name,
        )
        .limit(limit * 3)
        .all()
    )

    # Anything sharing a composition with what was found is a substitute for
    # it, whether or not its own name matches. Somebody asking for a brand
    # the shop has run out of wants the equivalent that is on the shelf, and
    # having to search a second time for a name they do not know is the
    # thing this avoids.
    # Position in the query's own order, kept as a lookup. `rank()` runs once
    # per comparison inside the sort, and `named.index(row)` there would walk
    # the list every time — quadratic work for a number already known here.
    named_rank = {row.id: i for i, row in enumerate(named)}
    named_ids = set(named_rank)
    composition_ids = {row.composition_id for row in named if row.composition_id}
    alternatives: list[Medicine] = []
    if composition_ids:
        alternatives = (
            base.filter(
                Medicine.composition_id.in_(composition_ids),
                Medicine.id.notin_(named_ids),
            )
            .order_by(Medicine.brand_name)
            .limit(limit * 3)
            .all()
        )

    rows = named + alternatives
    stock = inventory.stock_snapshot(db, user.store_id, [r.id for r in rows], settings)

    def rank(row: Medicine) -> tuple:
        """What the counter needs, in the order it needs it.

        Three tiers: what was asked for and can be handed over, then an
        equivalent that can be handed over, then everything that cannot.
        Stock decides the tier because a brand that is out is not an answer
        to "do you have it?" — it is only worth showing so nobody searches
        for it twice.
        """
        state = stock[row.id]
        in_stock = state.available_units > 0
        is_named = row.id in named_ids
        tier = 0 if (in_stock and is_named) else 1 if in_stock else 2
        # Within a tier: named matches keep the brand-prefix order the query
        # produced; substitutes lead with the cheapest per unit, which is the
        # only reason to offer one.
        if is_named:
            return (tier, 0, named_rank[row.id], row.brand_name)
        return (tier, 1, state.unit_mrp or Decimal("999999"), row.brand_name)

    ordered = sorted(rows, key=rank)[:limit]
    return [
        to_search_result(r, settings, stock[r.id], is_alternative=r.id not in named_ids)
        for r in ordered
    ]


@router.get("/match", response_model=MedicineMatchResult)
def match_medicines(
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
    q: str = Query(min_length=1, description="The product name as the supplier wrote it"),
    limit: int = Query(12, ge=1, le=50),
    item_type: ItemType | None = None,
) -> MedicineMatchResult:
    """Which product a line of a supplier's invoice might mean.

    A distributor writes `PARACETAMOL TAB`, `VOLINI GEL 30GM`, `AMOXY 500
    CAP 10'S` — none of which is what the master holds. Matching is on
    tokens rather than the whole string, so a trailing form word or a
    different order stops mattering.

    **It never chooses.** The answer is a ranked list plus what was noticed
    about the text, and the operator picks one or says none of them is it.
    A pack size on the line is read out and checked separately, because
    `Volini Gel 10GM` and `Volini Gel 30GM` are two products: a candidate
    whose name matches but whose pack does not comes back flagged, and
    `has_pack_match: false` means the size is not on the master at all.
    """
    result = matching.find(
        db,
        store_id=user.store_id,
        text=q,
        limit=limit,
        item_type=item_type.value if item_type is not None else None,
    )
    stock = inventory.stock_snapshot(
        db, user.store_id, [c.medicine.id for c in result.candidates], settings
    )
    return MedicineMatchResult(
        query=result.query,
        terms=result.terms,
        pack_hint=(
            PackHintOut(
                text=result.pack_hint.text,
                size=result.pack_hint.size,
                unit=result.pack_hint.unit,
            )
            if result.pack_hint
            else None
        ),
        form_hint=(
            FormHintOut(text=result.form_hint.text, dosage_form=result.form_hint.dosage_form)
            if result.form_hint
            else None
        ),
        matches=[
            MedicineMatch(
                medicine=to_search_result(c.medicine, settings, stock[c.medicine.id]),
                score=round(c.score, 4),
                pack_matches=c.pack_matches,
                form_matches=c.form_matches,
                note=c.note,
            )
            for c in result.candidates
        ],
        has_pack_match=result.has_pack_match,
        has_form_match=result.has_form_match,
    )


@router.get("/{medicine_id}/alternatives", response_model=list[MedicineSearchResult])
def medicine_alternatives(
    medicine_id: UUID,
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
    limit: int = Query(20, ge=1, le=50),
) -> list[MedicineSearchResult]:
    """Same salt composition, different brand — the substitution panel.

    Ordered cheapest first by price per sellable unit, because the whole
    point of the panel is to offer the customer a cheaper equivalent.
    Unpriced items sort last rather than masquerading as free.
    """
    medicine = db.get(Medicine, medicine_id)
    if medicine is None or medicine.store_id != user.store_id:
        raise NotFoundError("Medicine not found.")

    # Substitution is a statement about the formulation. Two general items
    # are not equivalents just because neither has one, so a general item has
    # no alternatives rather than *every* general item as an alternative.
    if medicine.composition_id is None:
        return []

    rows = (
        _loaded(db.query(Medicine))
        .filter(
            Medicine.store_id == user.store_id,
            Medicine.composition_id == medicine.composition_id,
            Medicine.id != medicine.id,
            Medicine.is_active.is_(True),
        )
        .limit(limit)
        .all()
    )
    stock = inventory.stock_snapshot(db, user.store_id, [r.id for r in rows], settings)

    def by_unit_price(row: Medicine):
        state = stock[row.id]
        # Out-of-stock and unpriced items sort last: a substitute you cannot
        # hand over is not a substitute.
        return (
            state.unit_mrp is None,
            state.available_units <= 0,
            state.unit_mrp or Decimal("0"),
            row.brand_name,
        )

    return [
        to_search_result(r, settings, stock[r.id]) for r in sorted(rows, key=by_unit_price)
    ]


# --- crud ----------------------------------------------------------------


@router.get("", response_model=Page[MedicineOut])
def list_medicines(
    user: CurrentUser,
    settings: CurrentSettings,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    item_type: ItemType | None = None,
    composition_id: UUID | None = None,
    manufacturer_id: UUID | None = None,
    is_active: bool | None = None,
) -> Page[MedicineOut]:
    query = _loaded(db.query(Medicine)).filter(Medicine.store_id == user.store_id)

    if q:
        needle = f"%{catalog.normalise_name(q).lower()}%"
        query = query.filter(Medicine.search_text.like(needle))
    if item_type is not None:
        query = query.filter(Medicine.item_type == item_type.value)
    if composition_id is not None:
        query = query.filter(Medicine.composition_id == composition_id)
    if manufacturer_id is not None:
        query = query.filter(Medicine.manufacturer_id == manufacturer_id)
    if is_active is not None:
        query = query.filter(Medicine.is_active.is_(is_active))

    total = query.count()
    rows = (
        query.order_by(Medicine.brand_name).offset(params.offset).limit(params.page_size).all()
    )
    stock = inventory.stock_snapshot(db, user.store_id, [r.id for r in rows], settings)
    return Page.build([_to_out(r, settings, stock[r.id]) for r in rows], total, params)


# --- bulk import ---------------------------------------------------------

# Money columns are deliberately absent: MRP, GST rate and discount limits
# arrive with each consignment, so they are entered against a batch.
STANDARD_COLUMNS = (
    Column("brand_name", True, "The brand as it is printed on the pack.", "Dolo 650"),
    Column("manufacturer", True, "Added to the manufacturer list if it is new.", "Micro Labs"),
    Column(
        "composition",
        True,
        "Ingredients separated by +. Re-canonicalised, so order and spacing do not matter.",
        "Paracetamol 650mg",
    ),
    Column("therapeutic_class", False, "What it is used for.", "Analgesic"),
    Column(
        "dosage_form",
        False,
        "TABLET, CAPSULE, SYRUP, INJECTION, OINTMENT, DROPS, POWDER or OTHER. "
        "Defaults to TABLET. Only tablets and capsules are sellable piece by piece.",
        "TABLET",
    ),
    Column("pack_size", False, "How much is in one pack: 10 tablets, 100 ml, 20 gm.", "15"),
    Column(
        "pack_unit",
        False,
        "TABLET, CAPSULE, ML, GM, IU, VIAL or UNIT — has to suit the dosage form. "
        "Left blank, the form's usual unit is used.",
        "TABLET",
    ),
    Column("pack_label", False, "Left blank, one is composed: “15 tab strip”.", "15 tab strip"),
    Column(
        "schedule_type",
        False,
        "OTC, SCHEDULE_H, SCHEDULE_H1, SCHEDULE_X or OTHER. H, H1 and X mark the medicine "
        "prescription-only, and the counter will refuse to bill it without a doctor.",
        "SCHEDULE_H",
    ),
    Column("hsn_code", False, "GST classification code.", "30049099"),
    Column(
        "list_mrp_per_pack",
        False,
        "The price list's MRP, kept for reference only. A sale is always billed from the "
        "MRP on the batch being sold, which is the figure printed on the box.",
        "35.50",
    ),
    Column("reorder_level_units", False, "Units at which the order book raises it.", "100"),
    Column("rack_location", False, "Where it sits in the shop.", "A1"),
)

PRICE_LIST_COLUMNS = (
    Column("Medicine Name", True, "The brand name.", "Dolo 650"),
    Column(
        "Price (INR)",
        False,
        "MRP for the whole pack. Kept as a reference price only — a sale is billed from "
        "the MRP on the batch being sold.",
        "35.50",
    ),
    Column("Manufacturer", True, "Added to the manufacturer list if it is new.", "Micro Labs"),
    Column("Salt/Composition", True, "Ingredients separated by +.", "Paracetamol 650mg"),
    Column(
        "Pack Info",
        False,
        "The pack as the price list writes it. Translated into a dosage form, a pack size "
        "and the unit it is measured in.",
        "strip of 15 tablets",
    ),
    Column(
        "Rx Required",
        False,
        "Yes/No. Yes records the medicine as Schedule H — prescription-only. If it is "
        "actually Schedule H1 or X, set that on the medicine afterwards.",
        "Yes",
    ),
)

#: General goods — soap, nappies, a thermometer. No composition, no
#: schedule, no dosage form: none of them mean anything for a bar of soap,
#: and asking for them would only invite something meaningless to be typed.
GENERAL_COLUMNS = (
    Column("brand_name", True, "The item as it is printed on the pack.", "Dettol Soap"),
    Column("manufacturer", True, "Added to the maker list if it is new.", "Reckitt"),
    Column(
        "pack_unit",
        False,
        "PIECE for countable articles (a box of soaps), or ML / GM for a measured "
        "container. Defaults to PIECE.",
        "PIECE",
    ),
    Column(
        "pack_size",
        False,
        "How many pieces in the pack, or the volume/weight of the container. A pack of "
        "12 sells as 12, so one soap can go out of a box.",
        "12",
    ),
    Column("pack_label", False, "Left blank, it is composed from the two above.", ""),
    Column("hsn_code", False, "Tax classification, if you have it.", "3401"),
    Column("list_mrp_per_pack", False, "Reference only — a sale bills the batch's MRP.", "290"),
    Column("reorder_level_units", False, "Units at which it counts as low stock.", "24"),
    Column("rack_location", False, "Where it sits in the shop.", "Front-2"),
)

LAYOUTS = {
    "standard": STANDARD_COLUMNS,
    "price_list": PRICE_LIST_COLUMNS,
    "general": GENERAL_COLUMNS,
}

# What each layout's fields may be called. The first name is the canonical
# one; the rest are what real exports happen to use.
STANDARD_ALIASES = {c.name: (c.name,) for c in STANDARD_COLUMNS} | {
    "brand_name": ("brand_name", "medicine_name", "product_name", "name"),
    "composition": ("composition", "salt_composition", "salt", "generic_name"),
    "list_mrp_per_pack": ("list_mrp_per_pack", "mrp_per_pack", "mrp", "price_inr", "price"),
}
GENERAL_ALIASES = {c.name: (c.name,) for c in GENERAL_COLUMNS} | {
    "brand_name": ("brand_name", "item_name", "product_name", "name"),
    "manufacturer": ("manufacturer", "maker", "brand_owner", "company", "marketer"),
    "list_mrp_per_pack": ("list_mrp_per_pack", "mrp_per_pack", "mrp", "price"),
}
PRICE_LIST_ALIASES = {
    "brand_name": ("medicine_name", "brand_name", "product_name", "name"),
    "list_mrp_per_pack": ("price_inr", "price", "mrp", "mrp_per_pack"),
    "manufacturer": ("manufacturer", "marketer", "company"),
    "composition": ("salt_composition", "composition", "salt", "generic_name"),
    "pack_info": ("pack_info", "pack", "packing", "pack_size_info"),
    "rx_required": ("rx_required", "rx", "prescription_required", "prescription"),
}
REQUIRED_IMPORT_COLUMNS = ("brand_name", "manufacturer", "composition")

_YES = {"yes", "y", "true", "1", "rx", "required", "prescription", "prescription required"}
_NO = {"no", "n", "false", "0", "otc", "not required", "non-rx", "nonrx", ""}


def _canonical(row: dict[str, str], aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Re-key one row onto the names the importer works in."""
    out = {}
    for canonical_name, names in aliases.items():
        for name in names:
            value = row.get(spreadsheet.normalise(name), "")
            if value:
                out[canonical_name] = value
                break
    return out


def _detect_layout(headers: list[str]) -> str:
    """Which layout a file is in, from its header row.

    A price list is recognised by the columns only it has, so an operator
    who picks the wrong one in the UI still gets their file read correctly.
    """
    present = set(headers)
    if "brand_name" in present and "composition" in present:
        return "standard"
    if present & {"medicine_name", "pack_info", "price_inr", "salt_composition"}:
        return "price_list"
    return "standard"


def _prescription_flag(value: str) -> bool | None:
    text = (value or "").strip().lower()
    if text in _YES:
        return True
    if text in _NO:
        return False
    raise AppError(
        f"“{value}” is not a yes or a no.", code="VALIDATION_ERROR", field="rx_required"
    )


GENERAL_GUIDANCE = (
    "One general item per row — soap, nappies, a thermometer. Only brand_name and "
    "manufacturer have to be filled in.",
    "No composition and no schedule: a bar of soap has neither, and the importer will "
    "not ask for them.",
    "pack_unit PIECE means the pack holds countable articles, so a pack_size of 12 sells "
    "as twelve and one soap can go out of the box. ML or GM means one container, however "
    "much is in it.",
    "An item already on file with the same maker and pack is reported as skipped. Tick "
    "“update rows that already exist” to overwrite it from the file instead.",
    "MRP, GST rate and cost are not here: they arrive with each consignment and are "
    "entered against the batch.",
)

STANDARD_GUIDANCE = (
    "One medicine per row. brand_name, manufacturer and composition are the only columns "
    "that have to be filled in.",
    "A brand already on file with the same manufacturer and pack is reported as skipped. "
    "Tick “update rows that already exist” to overwrite it from the file instead.",
    "Manufacturers, compositions and salts are all created as needed — there is no list to "
    "prepare first.",
    "MRP, GST rate and discounts are not here: they arrive with each consignment and are "
    "entered against the batch.",
)

PRICE_LIST_GUIDANCE = (
    "This is the layout a manufacturer's or portal's price list comes in. Only Medicine "
    "Name, Manufacturer and Salt/Composition have to be filled in.",
    "Pack Info is read as written — “strip of 10 tablets”, “bottle of 30 ml Oral "
    "Suspension”, “vial of 1 Powder for Injection”. Where the phrase does not name a form, "
    "the medicine is filed as OTHER rather than guessed at.",
    "Only tablets and capsules end up sellable piece by piece. A 100 ml bottle is one "
    "sellable unit, not a hundred.",
    "Price (INR) is kept as a reference figure. Sales are billed from the MRP on the batch.",
)


@router.get("/import-template")
def medicine_import_template(
    admin: MedicineEditor,
    file_format: str = Query("csv", alias="format", pattern="^(csv|xlsx)$"),
    layout: str = Query("standard", pattern="^(standard|price_list|general)$"),
):
    """The upload template, as CSV or Excel, in either layout.

    Built from the same column list the importer validates against, so a
    downloaded template cannot drift out of step with what is accepted.
    """
    columns = LAYOUTS[layout]
    name = {
        "standard": "medicines-template",
        "price_list": "medicines-price-list-template",
        "general": "general-items-template",
    }[layout]
    guidance = {
        "standard": STANDARD_GUIDANCE,
        "price_list": PRICE_LIST_GUIDANCE,
        "general": GENERAL_GUIDANCE,
    }[layout]
    if file_format == "csv":
        return spreadsheet.csv_template(columns, filename=f"{name}.csv")
    return spreadsheet.xlsx_response(
        spreadsheet.xlsx_template(
            columns,
            title="General items" if layout == "general" else "Medicines",
            guidance=guidance,
        ),
        f"{name}.xlsx",
    )


@router.get("/{medicine_id}", response_model=MedicineOut)
def get_medicine(
    medicine_id: UUID, user: CurrentUser, settings: CurrentSettings, db: DbSession
) -> MedicineOut:
    medicine = _loaded(db.query(Medicine)).filter(Medicine.id == medicine_id).one_or_none()
    if medicine is None or medicine.store_id != user.store_id:
        raise NotFoundError("Medicine not found.")
    stock = inventory.stock_snapshot(db, user.store_id, [medicine.id], settings)
    return _to_out(medicine, settings, stock[medicine.id])


@router.post("", response_model=MedicineOut, status_code=status.HTTP_201_CREATED)
def create_medicine(
    payload: MedicineCreate, request: Request, admin: MedicineAuthor, db: DbSession
) -> MedicineOut:
    settings = get_store_settings(db, admin.store_id)
    manufacturer = _resolve_manufacturer(
        db, admin.store_id, payload.manufacturer_id, payload.manufacturer_name, admin.id
    )
    # A general item has no formulation to resolve — the schema has already
    # cleared any the client sent.
    composition = (
        None
        if payload.item_type is ItemType.GENERAL
        else catalog.resolve_composition(
            db,
            composition_id=payload.composition_id,
            composition_text=payload.composition_text,
            therapeutic_class=payload.therapeutic_class,
            actor_id=admin.id,
        )
    )

    # The dosage form decides how the pack is measured and whether it is
    # countable at all — a syrup is one unit, a strip is ten, a box of soaps
    # is however many soaps.
    pack_size, pack_unit, units_per_pack, pack_label = catalog.resolve_pack(
        payload.dosage_form,
        payload.pack_size,
        payload.pack_unit.value if payload.pack_unit else None,
        payload.pack_label,
    )

    _assert_no_duplicate(
        db, admin.store_id, payload.brand_name.strip(), manufacturer.id, pack_label
    )

    medicine = Medicine(
        store_id=admin.store_id,
        brand_name=payload.brand_name.strip(),
        manufacturer_id=manufacturer.id,
        item_type=payload.item_type.value,
        composition_id=composition.id if composition is not None else None,
        dosage_form=payload.dosage_form.value,
        pack_size=pack_size,
        pack_unit=pack_unit,
        pack_label=pack_label,
        units_per_pack=units_per_pack,
        schedule_type=payload.schedule_type.value,
        is_prescription_required=payload.is_prescription_required
        or payload.schedule_type in (ScheduleType.SCHEDULE_H, ScheduleType.SCHEDULE_H1,
                                     ScheduleType.SCHEDULE_X),
        hsn_code=payload.hsn_code,
        list_mrp_per_pack=payload.list_mrp_per_pack,
        reorder_level_units=payload.reorder_level_units,
        rack_location=payload.rack_location,
        created_by_id=admin.id,
        updated_by_id=admin.id,
    )
    db.add(medicine)
    db.flush()
    catalog.refresh_search_text(db, medicine)

    audit.record(
        db, action=AuditAction.CREATE, entity_type="Medicine", entity_id=medicine.id, user=admin,
        after=audit.snapshot(medicine, AUDIT_FIELDS), request=request,
    )
    db.commit()

    medicine = _loaded(db.query(Medicine)).filter(Medicine.id == medicine.id).one()
    stock = inventory.stock_snapshot(db, admin.store_id, [medicine.id], settings)
    return _to_out(medicine, settings, stock[medicine.id])


@router.patch("/{medicine_id}", response_model=MedicineOut)
def update_medicine(
    medicine_id: UUID,
    payload: MedicineUpdate,
    request: Request,
    admin: MedicineEditor,
    db: DbSession,
) -> MedicineOut:
    settings = get_store_settings(db, admin.store_id)
    medicine = db.get(Medicine, medicine_id)
    if medicine is None or medicine.store_id != admin.store_id:
        raise NotFoundError("Medicine not found.")

    before = audit.snapshot(medicine, AUDIT_FIELDS)
    changes = payload.model_dump(exclude_unset=True)

    composition_text = changes.pop("composition_text", None)
    if medicine.is_general:
        # Soap acquires no salt by being edited. `item_type` itself is not
        # editable: changing it would strand a composition, and re-deriving
        # the pack from a different set of rules would restate stock that has
        # already been received.
        changes.pop("composition_id", None)
        composition_text = None
    elif composition_text and changes.get("composition_id") is None:
        changes["composition_id"] = catalog.resolve_composition(
            db, composition_text=composition_text, actor_id=admin.id
        ).id

    pack_keys = {"dosage_form", "pack_size", "pack_unit", "pack_label"}
    touches_pack = bool(pack_keys & changes.keys())

    for field, value in changes.items():
        setattr(medicine, field, value.value if hasattr(value, "value") else value)
    medicine.updated_by_id = admin.id

    if touches_pack:
        # Re-derive the whole pack: switching TABLET -> SYRUP must not leave
        # units_per_pack at 10, which would let the POS sell a tenth of a bottle.
        if "pack_unit" in changes:
            unit = medicine.pack_unit
        elif "dosage_form" in changes:
            unit = None  # old unit may not suit the new form; take its default
        else:
            unit = medicine.pack_unit  # keep a deliberate choice like VIAL

        label = changes.get("pack_label")
        if label is None and "dosage_form" not in changes and "pack_size" not in changes:
            label = medicine.pack_label

        (
            medicine.pack_size,
            medicine.pack_unit,
            medicine.units_per_pack,
            medicine.pack_label,
        ) = catalog.resolve_pack(medicine.dosage_form, medicine.pack_size, unit, label)

    if {"brand_name", "manufacturer_id", "pack_label"} & changes.keys():
        _assert_no_duplicate(
            db, medicine.store_id, medicine.brand_name, medicine.manufacturer_id,
            medicine.pack_label, exclude_id=medicine.id,
        )

    db.flush()
    catalog.refresh_search_text(db, medicine)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Medicine", entity_id=medicine.id, user=admin,
        before=before, after=audit.snapshot(medicine, AUDIT_FIELDS), request=request,
    )
    db.commit()

    medicine = _loaded(db.query(Medicine)).filter(Medicine.id == medicine.id).one()
    stock = inventory.stock_snapshot(db, admin.store_id, [medicine.id], settings)
    return _to_out(medicine, settings, stock[medicine.id])


@router.delete("/{medicine_id}", response_model=DeleteResult)
def delete_medicine(
    medicine_id: UUID, request: Request, admin: MedicineRemover, db: DbSession
) -> DeleteResult:
    """Removes a medicine that was never stocked.

    Once a batch exists the medicine is part of the stock record — and, after
    Phase 4, of sales history — so it is deactivated instead of erased. A
    deactivated medicine disappears from search but its history stays intact.
    """
    medicine = db.get(Medicine, medicine_id)
    if medicine is None or medicine.store_id != admin.store_id:
        raise NotFoundError("Medicine not found.")

    batch_count = (
        db.query(func.count(Batch.id)).filter(Batch.medicine_id == medicine.id).scalar()
    )
    before = audit.snapshot(medicine, AUDIT_FIELDS)

    if batch_count:
        if not medicine.is_active:
            raise ConflictError(
                f"{medicine.brand_name} has {batch_count} batch(es) on record and is "
                f"already inactive; its stock history cannot be erased.",
                code="MEDICINE_IN_USE",
            )
        medicine.is_active = False
        medicine.updated_by_id = admin.id
        audit.record(
            db, action=AuditAction.UPDATE, entity_type="Medicine", entity_id=medicine.id,
            user=admin, before=before, after=audit.snapshot(medicine, AUDIT_FIELDS),
            reason="Deactivated instead of deleted — stock history exists.", request=request,
        )
        db.commit()
        return DeleteResult(
            deleted=False,
            id=medicine_id,
            message=(
                f"{medicine.brand_name} has {batch_count} batch(es), so it was deactivated "
                f"rather than deleted. It will no longer appear at the counter."
            ),
        )

    db.delete(medicine)
    audit.record(
        db, action=AuditAction.DELETE, entity_type="Medicine", entity_id=medicine_id,
        user=admin, before=before, request=request,
    )
    db.commit()
    return DeleteResult(deleted=True, id=medicine_id, message="Medicine deleted.")


@router.post("/import", response_model=ImportResult)
async def import_medicines(
    request: Request,
    admin: MedicineEditor,
    db: DbSession,
    file: UploadFile = File(..., description="CSV or XLSX with a header row"),
    update_existing: bool = Query(False, description="Update rows that already exist"),
    layout: str = Query(
        "auto",
        pattern="^(auto|standard|price_list|general)$",
        description=(
            "Column layout; 'auto' reads it from the header row. 'general' loads "
            "non-medicine goods, which carry no composition or schedule."
        ),
    ),
) -> ImportResult:
    """Bulk-load the medicine master from a spreadsheet.

    Two layouts are accepted: the shop's own template, and the price-list
    shape a manufacturer or portal exports (Medicine Name, Price, Pack
    Info…). Each row is validated on its own savepoint, so a bad row is
    reported against its line number and the good rows around it still
    land — a 2000-row file is not lost to three typos.
    """
    raw = await file.read()
    spreadsheet.check_size(raw)
    headers, rows = spreadsheet.read_table(
        file.filename, raw, required=("brand_name", "medicine_name")
    )

    chosen = _detect_layout(headers) if layout == "auto" else layout
    aliases = {
        "standard": STANDARD_ALIASES,
        "price_list": PRICE_LIST_ALIASES,
        "general": GENERAL_ALIASES,
    }[chosen]

    # Soap has no salt, so a general file is not asked for one.
    required = (
        ("brand_name", "manufacturer")
        if chosen == "general"
        else REQUIRED_IMPORT_COLUMNS
    )
    present = set(headers)
    missing = [
        name
        for name in required
        if not (present & {spreadsheet.normalise(alias) for alias in aliases[name]})
    ]
    if missing:
        wanted = ", ".join(aliases[name][0] for name in missing)
        raise AppError(
            f"Missing required column(s): {wanted}.",
            code="MISSING_COLUMNS",
            field="file",
        )

    created = updated = skipped = 0
    errors: list[ImportRowError] = []
    warnings: list[str] = []
    prescription_rows = 0
    total = 0

    for sheet_row in rows:
        if sheet_row.is_blank:
            continue
        total += 1
        if total > spreadsheet.MAX_IMPORT_ROWS:
            errors.append(
                ImportRowError(
                    row=sheet_row.number,
                    message=f"Stopped at the {spreadsheet.MAX_IMPORT_ROWS}-row limit.",
                )
            )
            break

        row = _canonical(sheet_row.values, aliases)
        # A savepoint, not a full rollback: rolling the session back would
        # discard every good row imported before this one while still
        # counting them as created.
        savepoint = db.begin_nested()
        try:
            outcome, was_rx = _import_row(db, admin, row, update_existing, chosen)
            savepoint.commit()
        except AppError as exc:
            savepoint.rollback()
            errors.append(ImportRowError(row=sheet_row.number, field=exc.field, message=exc.message))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the file
            savepoint.rollback()
            errors.append(ImportRowError(row=sheet_row.number, message=str(exc)))
            continue

        prescription_rows += 1 if was_rx else 0
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1

    if chosen == "price_list" and prescription_rows:
        # Say what was assumed, because the stricter schedules carry a
        # register obligation this file cannot tell us about.
        warnings.append(
            f"{prescription_rows} row(s) marked Rx were recorded as Schedule H. "
            f"Any that are actually Schedule H1 or X need setting on the medicine."
        )

    audit.record(
        db, action=AuditAction.CREATE, entity_type="MedicineImport", user=admin,
        after={"layout": chosen, "created": created, "updated": updated,
               "skipped": skipped, "errors": len(errors)},
        request=request,
    )
    db.commit()

    return ImportResult(
        total_rows=total, created=created, updated=updated, skipped=skipped,
        errors=errors, warnings=warnings, layout=chosen,
    )


def _import_row(db, admin, row: dict, update_existing: bool, layout: str) -> tuple[str, bool]:
    """One row -> one product. Returns the outcome and whether it needs a script."""
    is_general = layout == "general"
    brand_name = row.get("brand_name", "")
    if not brand_name:
        raise AppError("brand_name is required.", code="VALIDATION_ERROR", field="brand_name")
    if not row.get("manufacturer"):
        raise AppError("manufacturer is required.", code="VALIDATION_ERROR", field="manufacturer")
    # Soap has no salt. Everything else on the master does.
    if not is_general and not row.get("composition"):
        raise AppError("composition is required.", code="VALIDATION_ERROR", field="composition")

    manufacturer = _resolve_manufacturer(
        db, admin.store_id, None, row["manufacturer"], admin.id
    )
    composition = (
        None
        if is_general
        else catalog.resolve_composition(
            db,
            composition_text=row["composition"],
            therapeutic_class=row.get("therapeutic_class") or None,
            actor_id=admin.id,
        )
    )

    if is_general:
        # A general item carries the GENERAL placeholder so pack resolution,
        # which keys off the dosage form, still has something to work with.
        dosage_form = DosageForm.GENERAL.value
        size_in = _decimal(row.get("pack_size"), None, "pack_size")
        unit_in = (
            _enum(row.get("pack_unit"), PackUnit, None, "pack_unit")
            if row.get("pack_unit")
            else PackUnit.PIECE.value
        )
        label_in = row.get("pack_label")
    elif row.get("pack_info"):
        # The price-list shape: one free-text phrase carries the form, the
        # size and the unit.
        pack = packinfo.parse_pack_info(row["pack_info"])
        dosage_form = pack.dosage_form.value
        size_in, unit_in, label_in = pack.pack_size, pack.pack_unit, pack.pack_label
    else:
        dosage_form = _enum(row.get("dosage_form"), DosageForm, DosageForm.TABLET, "dosage_form")
        size_in = _decimal(row.get("pack_size"), None, "pack_size")
        unit_in = (
            _enum(row.get("pack_unit"), PackUnit, None, "pack_unit")
            if row.get("pack_unit")
            else None
        )
        label_in = row.get("pack_label")

    pack_size, pack_unit, units_per_pack, pack_label = catalog.resolve_pack(
        dosage_form, size_in, unit_in, label_in
    )

    if is_general:
        # Nothing to check a prescription against.
        needs_script = False
        schedule_type = ScheduleType.OTC.value
    elif layout == "price_list":
        needs_script = _prescription_flag(row.get("rx_required", ""))
        schedule_type = (
            ScheduleType.SCHEDULE_H.value if needs_script else ScheduleType.OTC.value
        )
    else:
        schedule_type = _enum(
            row.get("schedule_type"), ScheduleType, ScheduleType.OTC, "schedule_type"
        )
        needs_script = schedule_type in (
            ScheduleType.SCHEDULE_H.value,
            ScheduleType.SCHEDULE_H1.value,
            ScheduleType.SCHEDULE_X.value,
        )

    existing = (
        db.query(Medicine)
        .filter(
            Medicine.store_id == admin.store_id,
            func.lower(Medicine.brand_name) == brand_name.lower(),
            Medicine.manufacturer_id == manufacturer.id,
            Medicine.pack_label == pack_label,
        )
        .one_or_none()
    )
    if existing is not None and not update_existing:
        return "skipped", needs_script

    fields = {
        "dosage_form": dosage_form,
        "pack_size": pack_size,
        "pack_unit": pack_unit,
        "pack_label": pack_label,
        "units_per_pack": units_per_pack,
        "schedule_type": schedule_type,
        "hsn_code": row.get("hsn_code") or None,
        "list_mrp_per_pack": _decimal(row.get("list_mrp_per_pack"), None, "list_mrp_per_pack"),
        "reorder_level_units": _decimal(row.get("reorder_level_units"), None,
                                        "reorder_level_units"),
        "rack_location": row.get("rack_location") or None,
    }
    if fields["list_mrp_per_pack"] is not None and fields["list_mrp_per_pack"] < 0:
        raise AppError(
            "Price cannot be negative.", code="VALIDATION_ERROR", field="list_mrp_per_pack"
        )

    if existing is not None:
        for key, value in fields.items():
            # A price list carries no rack or reorder level; leaving those
            # out of the file must not wipe what the shop has set.
            if value is None and key in ("list_mrp_per_pack", "reorder_level_units",
                                         "rack_location", "hsn_code"):
                continue
            setattr(existing, key, value)
        if composition is not None:
            existing.composition_id = composition.id
        existing.is_prescription_required = needs_script
        existing.updated_by_id = admin.id
        db.flush()
        catalog.refresh_search_text(db, existing)
        return "updated", needs_script

    medicine = Medicine(
        store_id=admin.store_id,
        brand_name=brand_name,
        manufacturer_id=manufacturer.id,
        item_type=(ItemType.GENERAL if is_general else ItemType.MEDICINE).value,
        composition_id=composition.id if composition is not None else None,
        is_prescription_required=needs_script,
        created_by_id=admin.id,
        updated_by_id=admin.id,
        **fields,
    )
    db.add(medicine)
    db.flush()
    catalog.refresh_search_text(db, medicine)
    return "created", needs_script


# --- helpers -------------------------------------------------------------


def _resolve_manufacturer(
    db, store_id: UUID, manufacturer_id: UUID | None, name: str | None, actor_id: UUID
) -> Manufacturer:
    if manufacturer_id is not None:
        manufacturer = db.get(Manufacturer, manufacturer_id)
        if manufacturer is None:
            raise NotFoundError("Manufacturer not found.", field="manufacturer_id")
        return manufacturer

    cleaned = (name or "").strip()
    if not cleaned:
        raise AppError("Manufacturer is required.", code="VALIDATION_ERROR", field="manufacturer")

    manufacturer = (
        db.query(Manufacturer)
        .filter(func.lower(Manufacturer.name) == cleaned.lower())
        .one_or_none()
    )
    if manufacturer is None:
        manufacturer = Manufacturer(name=cleaned, created_by_id=actor_id, updated_by_id=actor_id)
        db.add(manufacturer)
        db.flush()
    return manufacturer


def _assert_no_duplicate(
    db, store_id: UUID, brand_name: str, manufacturer_id: UUID, pack_label: str,
    exclude_id: UUID | None = None,
) -> None:
    query = db.query(Medicine).filter(
        Medicine.store_id == store_id,
        func.lower(Medicine.brand_name) == brand_name.lower(),
        Medicine.manufacturer_id == manufacturer_id,
        Medicine.pack_label == (pack_label or ""),
    )
    if exclude_id is not None:
        query = query.filter(Medicine.id != exclude_id)
    if query.count():
        raise ConflictError(
            "That brand, manufacturer and pack size already exists.",
            code="MEDICINE_EXISTS",
            field="brand_name",
        )


def _enum(value: str | None, enum_cls, default, field: str):
    if not value:
        return default.value if default is not None else None
    candidate = value.strip().upper().replace(" ", "_").replace("-", "_")
    try:
        return enum_cls(candidate).value
    except ValueError:
        allowed = ", ".join(m.value for m in enum_cls)
        raise AppError(
            f"{field} must be one of: {allowed}.", code="VALIDATION_ERROR", field=field
        ) from None


def _decimal(value: str | None, default, field: str):
    if not value:
        return default
    try:
        return Decimal(value)
    except InvalidOperation:
        raise AppError(f"{field} must be a number.", code="VALIDATION_ERROR", field=field) from None
