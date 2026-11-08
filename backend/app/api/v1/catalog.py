"""Salts, compositions and manufacturers.

Reads are open to every signed-in user (the counter needs them); writes are
user-only. These three tables are global reference data, not store-scoped.
"""

from uuid import UUID

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.core.deps import require_permission, CurrentUser, DbSession
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.models.catalog import Composition, CompositionSalt, Manufacturer, Medicine, Salt
from app.models.enums import AuditAction
from app.models.user import User
from app.schemas.catalog import (
    ComponentOut,
    CompositionCreate,
    CompositionOut,
    CompositionUpdate,
    DeleteResult,
    ImportResult,
    ImportRowError,
    ManufacturerCreate,
    ManufacturerOut,
    ManufacturerUpdate,
    SaltCreate,
    SaltOut,
    SaltUpdate,
    SimilarCompositions,
)
from app.schemas.common import Page, PageParams
from app.services import audit, catalog
from app.utils import spreadsheet
from app.utils.spreadsheet import Column

#: Admin *and* still holding Masters. The read routes above answer to the
#: counter as well (see api/v1/router.py); maintaining the data does not.
#: Formulations, salts and brand owners are reference data — no cost, no
#: price, nothing commercial — so either set can be handed to whoever keeps
#: the catalogue tidy. The module is the gate, with no role check behind it,
#: which is what makes granting one mean something.
#: Formulations and brand owners are reference data — no cost, no price,
#: nothing commercial — so the counter may add one it needs. Changing an
#: existing one re-points every medicine that shares it, which is a
#: different act.
CompositionAuthor = Annotated[User, Depends(require_permission("compositions:create"))]
CompositionEditor = Annotated[User, Depends(require_permission("compositions:edit"))]
ManufacturerAuthor = Annotated[
    User, Depends(require_permission("manufacturers:create"))
]
ManufacturerEditor = Annotated[User, Depends(require_permission("manufacturers:edit"))]

router = APIRouter()


# --- salts ---------------------------------------------------------------

salts_router = APIRouter()


@salts_router.get("", response_model=Page[SaltOut])
def list_salts(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    is_active: bool | None = None,
) -> Page[SaltOut]:
    query = db.query(Salt)
    if q:
        query = query.filter(Salt.name.like(f"%{catalog.normalise_name(q)}%"))
    if is_active is not None:
        query = query.filter(Salt.is_active.is_(is_active))

    total = query.count()
    rows = query.order_by(Salt.name).offset(params.offset).limit(params.page_size).all()
    return Page.build([SaltOut.model_validate(r) for r in rows], total, params)


@salts_router.post("", response_model=SaltOut, status_code=status.HTTP_201_CREATED)
def create_salt(payload: SaltCreate, request: Request, user: CompositionAuthor, db: DbSession) -> SaltOut:
    name = catalog.normalise_name(payload.name)
    if db.query(Salt).filter(Salt.name == name).count():
        raise ConflictError("That salt already exists.", code="SALT_EXISTS", field="name")

    salt = Salt(
        name=name,
        therapeutic_class=payload.therapeutic_class,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(salt)
    db.flush()
    audit.record(
        db, action=AuditAction.CREATE, entity_type="Salt", entity_id=salt.id, user=user,
        after=audit.snapshot(salt), request=request,
    )
    db.commit()
    db.refresh(salt)
    return SaltOut.model_validate(salt)


@salts_router.patch("/{salt_id}", response_model=SaltOut)
def update_salt(
    salt_id: UUID, payload: SaltUpdate, request: Request, user: CompositionEditor, db: DbSession
) -> SaltOut:
    salt = db.get(Salt, salt_id)
    if salt is None:
        raise NotFoundError("Salt not found.")

    before = audit.snapshot(salt)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        changes["name"] = catalog.normalise_name(changes["name"])
        clash = (
            db.query(Salt)
            .filter(Salt.name == changes["name"], Salt.id != salt.id)
            .count()
        )
        if clash:
            raise ConflictError("Another salt already uses that name.", field="name")

    for field, value in changes.items():
        setattr(salt, field, value)
    salt.updated_by_id = user.id
    db.flush()

    # A renamed salt changes every haystack that contains it.
    if "name" in changes:
        composition_ids = [
            cid
            for (cid,) in db.query(CompositionSalt.composition_id).filter(
                CompositionSalt.salt_id == salt.id
            )
        ]
        for composition_id in composition_ids:
            catalog.refresh_search_text_for_composition(db, composition_id)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Salt", entity_id=salt.id, user=user,
        before=before, after=audit.snapshot(salt), request=request,
    )
    db.commit()
    db.refresh(salt)
    return SaltOut.model_validate(salt)


# --- compositions --------------------------------------------------------

compositions_router = APIRouter()


def _composition_out(composition: Composition, medicine_count: int | None = None) -> CompositionOut:
    return CompositionOut(
        id=composition.id,
        composition_text=composition.composition_text,
        therapeutic_class=composition.therapeutic_class,
        notes=composition.notes,
        is_active=composition.is_active,
        components=[
            ComponentOut(salt_id=c.salt_id, name=c.salt.name, strength=c.strength)
            for c in composition.components
        ],
        medicine_count=medicine_count,
    )


@compositions_router.get("", response_model=Page[CompositionOut])
def list_compositions(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    is_active: bool | None = None,
) -> Page[CompositionOut]:
    query = db.query(Composition).options(
        selectinload(Composition.components).selectinload(CompositionSalt.salt)
    )
    if q:
        # Match on the space-free key so "paracetamol 500 mg" finds
        # "PARACETAMOL 500MG".
        query = query.filter(
            Composition.search_key.like(f"%{catalog.composition_search_key(q)}%")
        )
    if is_active is not None:
        query = query.filter(Composition.is_active.is_(is_active))

    total = query.count()
    rows = (
        query.order_by(Composition.composition_text)
        .offset(params.offset)
        .limit(params.page_size)
        .all()
    )

    counts = dict(
        db.query(Medicine.composition_id, func.count(Medicine.id))
        .filter(
            Medicine.composition_id.in_([r.id for r in rows]),
            Medicine.store_id == user.store_id,
        )
        .group_by(Medicine.composition_id)
        .all()
    )
    return Page.build(
        [_composition_out(r, counts.get(r.id, 0)) for r in rows], total, params
    )


@compositions_router.get("/similar", response_model=SimilarCompositions)
def similar_compositions(
    user: CurrentUser,
    db: DbSession,
    text: str = Query(min_length=2, description="Formulation being typed"),
    limit: int = Query(8, ge=1, le=25),
) -> SimilarCompositions:
    """Formulations that resemble what is being typed.

    Backs the live duplicate warning in the new-composition dialog: matching
    ignores spacing and case, so an operator cannot create
    "PARACETAMOL 500 MG" alongside an existing "PARACETAMOL 500MG" and
    silently split one set of alternatives into two.
    """
    exact, others = catalog.find_similar_compositions(db, text, limit=limit)

    ids = [c.id for c in others] + ([exact.id] if exact else [])
    counts = dict(
        db.query(Medicine.composition_id, func.count(Medicine.id))
        .filter(Medicine.composition_id.in_(ids), Medicine.store_id == user.store_id)
        .group_by(Medicine.composition_id)
        .all()
    )
    return SimilarCompositions(
        exact=_composition_out(exact, counts.get(exact.id, 0)) if exact else None,
        similar=[_composition_out(c, counts.get(c.id, 0)) for c in others],
    )


# --- bulk import ---------------------------------------------------------

# Only the formulation is required. A composition *is* its ingredients; the
# class and the note are labels someone may not have to hand while typing up
# a list of two hundred of them.
COMPOSITION_COLUMNS = (
    Column(
        "composition",
        True,
        "The formulation, one row each. Separate ingredients with +. Strengths are "
        "optional but keep brands apart — Paracetamol 500mg and Paracetamol 650mg are "
        "different compositions. Order, spacing and case do not matter: the row is "
        "re-canonicalised on import, so B + A lands on the same row as A + B.",
        "Amoxicillin 500mg + Clavulanic Acid 125mg",
    ),
    Column("therapeutic_class", False, "What it is used for.", "Antibiotic"),
    Column("notes", False, "Anything worth recording against the formulation.", "Paediatric"),
)

COMPOSITION_GUIDANCE = (
    "Fill in one row per formulation. Only the composition column has to be filled in.",
    "A formulation already on file is left alone and reported as skipped, so the same "
    "file can be uploaded twice without creating duplicates.",
    "Tick “fill in blanks on existing rows” at upload time to let this file add a "
    "therapeutic class or note to formulations that are already there.",
    "Salts are created automatically from the ingredients you type — there is no "
    "separate salt list to prepare first.",
)


@compositions_router.get("/import-template")
def composition_import_template(
    user: CompositionEditor,
    file_format: str = Query("csv", alias="format", pattern="^(csv|xlsx)$"),
):
    """The upload template, as CSV or Excel.

    Generated from the same column list the importer validates against, so a
    downloaded template cannot drift out of step with what is accepted.
    """
    if file_format == "csv":
        return spreadsheet.csv_template(COMPOSITION_COLUMNS, filename="compositions-template.csv")
    return spreadsheet.xlsx_response(
        spreadsheet.xlsx_template(
            COMPOSITION_COLUMNS, title="Compositions", guidance=COMPOSITION_GUIDANCE
        ),
        "compositions-template.xlsx",
    )


@compositions_router.post("/import", response_model=ImportResult)
async def import_compositions(
    request: Request,
    user: CompositionEditor,
    db: DbSession,
    file: UploadFile = File(..., description="CSV or XLSX with a header row"),
    update_existing: bool = Query(
        False, description="Fill in the class and notes on formulations already on file"
    ),
) -> ImportResult:
    """Bulk-load formulations from the template.

    Each row is committed to its own savepoint: a typo on row 40 is reported
    against row 40 and the other 199 rows still land.
    """
    raw = await file.read()
    spreadsheet.check_size(raw)
    headers, rows = spreadsheet.read_table(file.filename, raw, required=("composition",))

    missing = spreadsheet.missing_columns(headers, COMPOSITION_COLUMNS)
    if missing:
        raise AppError(
            f"Missing required column(s): {', '.join(missing)}.",
            code="MISSING_COLUMNS",
            field="file",
        )

    created = updated = skipped = 0
    errors: list[ImportRowError] = []
    total = 0

    for row in rows:
        if row.is_blank:
            continue
        total += 1
        if total > spreadsheet.MAX_IMPORT_ROWS:
            errors.append(
                ImportRowError(
                    row=row.number,
                    message=f"Stopped at the {spreadsheet.MAX_IMPORT_ROWS}-row limit.",
                )
            )
            break

        savepoint = db.begin_nested()
        try:
            outcome = _import_composition_row(db, user, row, update_existing)
            savepoint.commit()
        except AppError as exc:
            savepoint.rollback()
            errors.append(ImportRowError(row=row.number, field=exc.field, message=exc.message))
            continue
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the file
            savepoint.rollback()
            errors.append(ImportRowError(row=row.number, message=str(exc)))
            continue

        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1

    audit.record(
        db, action=AuditAction.CREATE, entity_type="CompositionImport", user=user,
        after={"created": created, "updated": updated, "skipped": skipped, "errors": len(errors)},
        request=request,
    )
    db.commit()

    return ImportResult(
        total_rows=total, created=created, updated=updated, skipped=skipped, errors=errors
    )


def _import_composition_row(db, user, row, update_existing: bool) -> str:
    text = row.get("composition")
    if not text:
        raise AppError("composition is required.", code="VALIDATION_ERROR", field="composition")

    therapeutic_class = row.get("therapeutic_class") or None
    notes = row.get("notes") or None

    components = catalog.parse_composition_text(text)
    if not components:
        raise AppError(
            "That does not read as a formulation.",
            code="VALIDATION_ERROR",
            field="composition",
        )

    canonical = catalog.canonical_text(components)
    existing = (
        db.query(Composition).filter(Composition.composition_text == canonical).one_or_none()
    )
    if existing is not None:
        # Already on file. Only fill in what is blank — an upload must never
        # overwrite a class someone chose deliberately.
        if not update_existing:
            return "skipped"
        changed = False
        if therapeutic_class and not existing.therapeutic_class:
            existing.therapeutic_class = therapeutic_class
            changed = True
        if notes and not existing.notes:
            existing.notes = notes
            changed = True
        if not changed:
            return "skipped"
        existing.updated_by_id = user.id
        db.flush()
        return "updated"

    composition = catalog.resolve_composition(
        db,
        components=components,
        therapeutic_class=therapeutic_class,
        actor_id=user.id,
    )
    if notes:
        composition.notes = notes
        db.flush()
    return "created"


@compositions_router.get("/{composition_id}", response_model=CompositionOut)
def get_composition(composition_id: UUID, user: CurrentUser, db: DbSession) -> CompositionOut:
    composition = db.get(Composition, composition_id)
    if composition is None:
        raise NotFoundError("Composition not found.")
    count = (
        db.query(func.count(Medicine.id))
        .filter(Medicine.composition_id == composition.id, Medicine.store_id == user.store_id)
        .scalar()
    )
    return _composition_out(composition, count)


@compositions_router.post("", response_model=CompositionOut, status_code=status.HTTP_201_CREATED)
def create_composition(
    payload: CompositionCreate, request: Request, user: CompositionAuthor, db: DbSession
) -> CompositionOut:
    components = (
        [(c.name, c.strength) for c in payload.components] if payload.components else None
    )
    if components:
        components = [catalog.parse_component(f"{n} {s or ''}") for n, s in components]

    composition = catalog.resolve_composition(
        db,
        composition_text=payload.composition_text,
        components=components,
        therapeutic_class=payload.therapeutic_class,
        actor_id=user.id,
    )
    if payload.notes and not composition.notes:
        composition.notes = payload.notes

    audit.record(
        db, action=AuditAction.CREATE, entity_type="Composition", entity_id=composition.id,
        user=user, after=audit.snapshot(composition), request=request,
    )
    db.commit()
    db.refresh(composition)
    return _composition_out(composition, 0)


@compositions_router.patch("/{composition_id}", response_model=CompositionOut)
def update_composition(
    composition_id: UUID,
    payload: CompositionUpdate,
    request: Request,
    user: CompositionEditor,
    db: DbSession,
) -> CompositionOut:
    composition = db.get(Composition, composition_id)
    if composition is None:
        raise NotFoundError("Composition not found.")

    before = audit.snapshot(composition)
    changes = payload.model_dump(exclude_unset=True)

    # Reformulating re-groups substitutes, so it is handled apart from the
    # plain descriptive fields.
    raw_components = changes.pop("components", None)
    composition_text = changes.pop("composition_text", None)
    components = None
    if raw_components:
        components = [
            catalog.parse_component(f"{c['name']} {c.get('strength') or ''}")
            for c in raw_components
        ]
    elif composition_text:
        components = catalog.parse_composition_text(composition_text)

    if components:
        canonical = catalog.canonical_text(components)
        clash = (
            db.query(Composition)
            .filter(Composition.composition_text == canonical, Composition.id != composition.id)
            .one_or_none()
        )
        if clash is not None:
            raise ConflictError(
                f"“{canonical}” already exists. Move the brands onto that "
                f"composition rather than keeping a second copy.",
                code="COMPOSITION_EXISTS",
                field="composition_text",
            )
        catalog.set_components(db, composition, components, user.id)

    for field, value in changes.items():
        setattr(composition, field, value)
    composition.updated_by_id = user.id
    db.flush()

    # A changed formulation must reach every affected medicine's haystack.
    if components:
        catalog.refresh_search_text_for_composition(db, composition.id)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Composition", entity_id=composition.id,
        user=user, before=before, after=audit.snapshot(composition), request=request,
    )
    db.commit()
    db.refresh(composition)
    return _composition_out(composition)


@compositions_router.delete("/{composition_id}", response_model=DeleteResult)
def delete_composition(
    composition_id: UUID, request: Request, user: CompositionEditor, db: DbSession
) -> DeleteResult:
    """Removes a composition, provided no medicine still refers to it."""
    composition = db.get(Composition, composition_id)
    if composition is None:
        raise NotFoundError("Composition not found.")

    in_use = (
        db.query(func.count(Medicine.id))
        .filter(Medicine.composition_id == composition.id)
        .scalar()
    )
    if in_use:
        raise ConflictError(
            f"{in_use} medicine(s) still use this composition. Move them elsewhere "
            f"first, or mark this composition inactive.",
            code="COMPOSITION_IN_USE",
        )

    before = audit.snapshot(composition)
    db.delete(composition)

    audit.record(
        db, action=AuditAction.DELETE, entity_type="Composition", entity_id=composition_id,
        user=user, before=before, request=request,
    )
    db.commit()
    return DeleteResult(deleted=True, id=composition_id, message="Composition deleted.")


# --- manufacturers -------------------------------------------------------

manufacturers_router = APIRouter()


@manufacturers_router.get("", response_model=Page[ManufacturerOut])
def list_manufacturers(
    user: CurrentUser,
    db: DbSession,
    params: PageParams = Depends(),
    q: str | None = None,
    is_active: bool | None = None,
) -> Page[ManufacturerOut]:
    query = db.query(Manufacturer)
    if q:
        query = query.filter(func.lower(Manufacturer.name).like(f"%{q.strip().lower()}%"))
    if is_active is not None:
        query = query.filter(Manufacturer.is_active.is_(is_active))

    total = query.count()
    rows = query.order_by(Manufacturer.name).offset(params.offset).limit(params.page_size).all()
    return Page.build([ManufacturerOut.model_validate(r) for r in rows], total, params)


@manufacturers_router.post("", response_model=ManufacturerOut, status_code=status.HTTP_201_CREATED)
def create_manufacturer(
    payload: ManufacturerCreate, request: Request, user: ManufacturerAuthor, db: DbSession
) -> ManufacturerOut:
    name = payload.name.strip()
    if db.query(Manufacturer).filter(func.lower(Manufacturer.name) == name.lower()).count():
        raise ConflictError(
            "That manufacturer already exists.", code="MANUFACTURER_EXISTS", field="name"
        )

    manufacturer = Manufacturer(
        name=name,
        address=payload.address,
        phone=payload.phone,
        email=payload.email,
        created_by_id=user.id,
        updated_by_id=user.id,
    )
    db.add(manufacturer)
    db.flush()
    audit.record(
        db, action=AuditAction.CREATE, entity_type="Manufacturer", entity_id=manufacturer.id,
        user=user, after=audit.snapshot(manufacturer), request=request,
    )
    db.commit()
    db.refresh(manufacturer)
    return ManufacturerOut.model_validate(manufacturer)


@manufacturers_router.patch("/{manufacturer_id}", response_model=ManufacturerOut)
def update_manufacturer(
    manufacturer_id: UUID,
    payload: ManufacturerUpdate,
    request: Request,
    user: ManufacturerEditor,
    db: DbSession,
) -> ManufacturerOut:
    manufacturer = db.get(Manufacturer, manufacturer_id)
    if manufacturer is None:
        raise NotFoundError("Manufacturer not found.")

    before = audit.snapshot(manufacturer)
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"]:
        changes["name"] = changes["name"].strip()
        clash = (
            db.query(Manufacturer)
            .filter(
                func.lower(Manufacturer.name) == changes["name"].lower(),
                Manufacturer.id != manufacturer.id,
            )
            .count()
        )
        if clash:
            raise ConflictError("Another manufacturer already uses that name.", field="name")

    for field, value in changes.items():
        setattr(manufacturer, field, value)
    manufacturer.updated_by_id = user.id
    db.flush()

    if "name" in changes:
        catalog.refresh_search_text_for_manufacturer(db, manufacturer.id)

    audit.record(
        db, action=AuditAction.UPDATE, entity_type="Manufacturer", entity_id=manufacturer.id,
        user=user, before=before, after=audit.snapshot(manufacturer), request=request,
    )
    db.commit()
    db.refresh(manufacturer)
    return ManufacturerOut.model_validate(manufacturer)


router.include_router(salts_router, prefix="/salts", tags=["catalog"])
router.include_router(compositions_router, prefix="/compositions", tags=["catalog"])
router.include_router(manufacturers_router, prefix="/manufacturers", tags=["catalog"])
