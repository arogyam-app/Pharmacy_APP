"""Catalogue rules: composition canonicalisation and search-text upkeep.

Everything that decides *which medicines are alternatives of each other*
lives here. Two medicines are substitutes exactly when they resolve to the
same `Composition`, so canonicalisation has to be strict: same ingredients at
the same strengths must always land on the same row, whatever order or
casing the operator typed.
"""

from uuid import UUID

import re
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.models.catalog import Composition, CompositionSalt, Manufacturer, Medicine, Salt
from app.models.enums import (
    COUNTABLE_FORMS,
    COUNTABLE_UNITS,
    PACK_UNITS_BY_FORM,
    DosageForm,
    PackUnit,
    default_pack_unit,
)

# The canonical rendering: "AMBROXOL (7.5MG/ML)". The strength is bracketed
# so that where the salt name ends and the dose begins is unambiguous —
# "LEVOSALBUTAMOL 0.25MG/ML" reads as one run of text, and a combination of
# three of them reads as a wall of it.
#
# Already-bracketed input is matched first, so canonical text re-parses to
# itself: every screen that reads a composition back and saves it again must
# land on the same row, or a formulation quietly splits in two.
#
# The closing bracket is optional. A formulation arrives half-typed, or
# pasted out of a spreadsheet cell that truncated it, far more often than it
# arrives perfect — and `PARACETAMOL (325 MG` is not ambiguous about what was
# meant. Refusing to read it would only mean the strength ends up glued to
# the salt name, which is the failure this whole module exists to avoid.
_BRACKETED_RE = re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<strength>[^()]+?)\s*\)?\s*$")

# "500mg", "1.5 G", "1GM", "10 ML", "500 IU", "0.5%", "7.5 mg/ml", "1% w/v"
#
# Two details here are load-bearing, and both were wrong before.
#
# `%` is matched *without* a trailing `\b`. A word boundary needs a word
# character on one side of it and `%` is not one, so "0.5%" and "1% w/v" have
# no boundary after the sign — the old pattern never matched a percentage at
# all, and every ophthalmic and topical strength was swallowed into the salt
# name.
#
# `gm` is listed, and listed before the bare `g`. "1GM" is how Indian
# distributors write a gram, and `g\b` cannot match it: `g` and `m` are both
# word characters, so there is no boundary between them. Every injection
# dosed in grams was losing its strength the same way.
# The unit letters may be spaced apart — "100 M G" is what a tired thumb
# produces, and it means milligrams.
_UNIT = r"(?:m\s*c\s*g|m\s*g|g\s*m|m\s*l|i\s*u|u\s*n\s*i\s*t\s*s?|g)"
_STRENGTH_RE = re.compile(
    r"^(?P<name>.*?)[\s\-]*"
    rf"(?P<strength>\d+(?:\.\d+)?\s*(?:{_UNIT}\b|%).*)$",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")

# --- tidying the inside of a strength ------------------------------------
#
# Three rules, applied in order, and between them they turn "( 100  M G  )"
# into "100MG" while leaving "1% W/V" alone. The distinction is that a space
# between two *alphanumerics* is somebody's typing, whereas a space after a
# sign like `%` separates the dose from a qualifier and carries meaning.
_SLASH_GAP_RE = re.compile(r"\s*/\s*")            # "MG / ML" -> "MG/ML"
_PERCENT_GAP_RE = re.compile(r"(?<=\d)\s+(?=%)")  # "1 %"     -> "1%"
_ALNUM_GAP_RE = re.compile(r"(?<=[0-9A-Z])\s+(?=[0-9A-Z])")  # "100 M G" -> "100MG"


class CompositionError(AppError):
    code = "COMPOSITION_INVALID"


def normalise_name(value: str) -> str:
    """Uppercase, single-spaced — the canonical form of a salt name."""
    return _WHITESPACE_RE.sub(" ", value or "").strip().upper()


def normalise_strength(value: str | None) -> str | None:
    """" 100  M G " -> "100MG";  "7.5 mg / ml" -> "7.5MG/ML";  "1 % w/v" -> "1% W/V".

    Spaces between alphanumerics are closed up, because inside a dose they
    are always slips of the keyboard: nobody means something different by
    "100 MG" than by "100MG". A space that follows a sign is kept, because
    there it separates the dose from a qualifier — "1% W/V" reads correctly
    and "1%W/V" does not.
    """
    if not value:
        return None
    text = _WHITESPACE_RE.sub(" ", value).strip().upper()
    text = _SLASH_GAP_RE.sub("/", text)
    text = _PERCENT_GAP_RE.sub("", text)
    return _ALNUM_GAP_RE.sub("", text) or None


def parse_component(text: str) -> tuple[str, str | None]:
    """"Amoxicillin 500 mg" -> ("AMOXICILLIN", "500MG").

    Brackets are honoured when present — `Ambroxol (7.5mg/ml)` states plainly
    where the name stops — and inferred from the first number when they are
    not, so anything typed the old way still parses.
    """
    cleaned = _WHITESPACE_RE.sub(" ", text or "").strip()
    if not cleaned:
        raise CompositionError("A composition component cannot be blank.")

    bracketed = _BRACKETED_RE.match(cleaned)
    if bracketed and bracketed.group("name").strip():
        return (
            normalise_name(bracketed.group("name")),
            normalise_strength(bracketed.group("strength")),
        )

    match = _STRENGTH_RE.match(cleaned)
    if match and match.group("name").strip():
        return normalise_name(match.group("name")), normalise_strength(match.group("strength"))
    return normalise_name(cleaned), None


def parse_composition_text(text: str) -> list[tuple[str, str | None]]:
    """Split "A 500mg + B 125mg" into its components.

    The same salt can legitimately appear more than once — a biphasic
    tablet combining an immediate- and a sustained-release portion of the
    same drug is typed as "Paracetamol 300mg + Paracetamol 700mg", not as
    two separate salts. `CompositionSalt` has one row per salt, so repeats
    of one name are collapsed into a single component with a combined
    strength, ordered deterministically so the input order never matters.
    """
    if not text or not text.strip():
        raise CompositionError("Composition cannot be blank.", field="composition")

    parts = re.split(r"\s*\+\s*", text.strip())
    components = [parse_component(part) for part in parts if part.strip()]

    grouped: dict[str, list[str | None]] = {}
    for name, strength in components:
        grouped.setdefault(name, []).append(strength)

    return [(name, _combine_strengths(strengths)) for name, strengths in grouped.items()]


def canonical_text(components: list[tuple[str, str | None]]) -> str:
    """"PARACETAMOL (325MG) + ACECLOFENAC (100MG)".

    **The strongest ingredient leads**, which is how a combination is read:
    the base drug first and the adjuncts after it. Ties, and any formulation
    whose strengths cannot be compared, fall back to alphabetical.

    Whatever the order, it is *decided here* rather than taken from what was
    typed — two operators entering the same combination the other way round
    have to land on one row, or the brands using it are never offered as
    alternatives to each other. Each strength is bracketed against its own
    salt so a three-part combination can be read at a glance; a salt with no
    stated strength is rendered bare rather than with empty brackets.
    """
    ordered = sorted(components, key=_ordering_key(components))
    return " + ".join(
        f"{name} ({strength})" if strength else name for name, strength in ordered
    )


def _ordering_key(components: list[tuple[str, str | None]]):
    """Sort by descending strength where that means anything, else by name.

    "Where that means anything" is the careful part. Milligrams and per-cent
    do not compare, so a formulation mixing them is ordered alphabetically
    rather than by a number that would be meaningless. Within one family the
    scale is normalised, so 1GM correctly outranks 500MG.
    """
    measured = [_measure(strength) for _, strength in components]

    comparable = (
        len(components) > 1
        and all(m is not None for m in measured)
        and len({family for family, _ in measured}) == 1   # one family throughout
    )
    if not comparable:
        return lambda component: (Decimal(0), component[0])

    def by_strength(component: tuple[str, str | None]):
        name, strength = component
        return -_measure(strength)[1], name

    return by_strength


#: Milligrams per unit. Only mass units are unified: `MG/ML` is a
#: concentration and `%` a proportion, and neither converts into the other.
_MASS_IN_MG = {"MCG": Decimal("0.001"), "MG": Decimal(1), "GM": Decimal(1000),
               "G": Decimal(1000)}
_LEADING_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(.*)$")


def _measure(strength: str | None) -> tuple[str, Decimal] | None:
    """"500MG" -> ("MASS", 500);  "7.5MG/ML" -> ("MG/ML", 7.5).

    The first element is the family — two strengths are only ever compared
    when theirs agree. `None` means there is no number to compare at all.
    """
    if not strength:
        return None
    match = _LEADING_NUMBER_RE.match(strength.strip())
    if not match:
        return None

    amount = Decimal(match.group(1))
    unit = match.group(2).strip()
    if unit in _MASS_IN_MG:
        return "MASS", amount * _MASS_IN_MG[unit]
    return unit, amount


def _combine_strengths(strengths: list[str | None]) -> str | None:
    """One salt typed more than once -> one combined strength string.

    Ordered strongest-first when every strength is present and comparable
    (same family, via `_measure`) so "700MG + 300MG" and "300MG + 700MG"
    collapse to one result regardless of which was typed first; falls back
    to alphabetical order otherwise, for the same reason. Exact repeats
    ("500MG" twice) collapse to a single occurrence.
    """
    distinct = list(dict.fromkeys(s for s in strengths if s))
    if not distinct:
        return None
    if len(distinct) == 1:
        return distinct[0]

    measured = [_measure(s) for s in distinct]
    comparable = all(m is not None for m in measured) and len({family for family, _ in measured}) == 1
    ordered = sorted(distinct, key=lambda s: -_measure(s)[1]) if comparable else sorted(distinct)
    return " + ".join(ordered)


def composition_search_key(text: str) -> str:
    """Uppercase, with spaces and brackets stripped.

    "Paracetamol 500 mg", "PARACETAMOL 500MG" and "PARACETAMOL (500MG)" all
    collapse to "PARACETAMOL500MG", so near-duplicates are findable while
    typing. Dropping the brackets is what lets a formulation stored before
    this format existed still match one typed today.
    """
    return re.sub(r"[\s()]+", "", text or "").upper()


def find_similar_compositions(
    db: Session, text: str, limit: int = 8
) -> tuple[Composition | None, list[Composition]]:
    """Existing formulations that look like `text`.

    Returns (exact_match, others). "Exact" means it canonicalises to the same
    row — the operator is about to re-create something that already exists.
    The rest are ranked by how many salts they share, so a combination shows
    up while its first ingredient is still being typed.
    """
    raw = (text or "").strip()
    if len(raw) < 2:
        return None, []

    try:
        components = parse_composition_text(raw)
    except CompositionError:
        components = [(normalise_name(raw), None)]

    exact = None
    canonical = canonical_text(components)
    if canonical:
        exact = (
            db.query(Composition)
            .filter(Composition.composition_text == canonical)
            .one_or_none()
        )

    # Substring on the space-free key, in both directions: a partly typed
    # formulation should still surface the complete one.
    key = composition_search_key(raw)
    salt_names = [name for name, _ in components if name]

    conditions = [Composition.search_key.like(f"%{key}%")]
    for name in salt_names:
        conditions.append(Composition.search_key.like(f"%{composition_search_key(name)}%"))

    candidates = (
        db.query(Composition)
        .filter(or_(*conditions))
        .filter(Composition.is_active.is_(True))
        .limit(limit * 4)
        .all()
    )

    wanted = {name for name in salt_names}

    def rank(composition: Composition) -> tuple:
        theirs = {component.salt.name for component in composition.components}
        shared = len(wanted & theirs)
        # More shared salts first; then the shorter formulation, which is
        # usually the closer match.
        return (-shared, len(composition.composition_text), composition.composition_text)

    others = [c for c in sorted(candidates, key=rank) if exact is None or c.id != exact.id]
    return exact, others[:limit]


def set_components(
    db: Session,
    composition: Composition,
    components: list[tuple[str, str | None]],
    actor_id: UUID | None = None,
) -> None:
    """Replace a composition's salts and recanonicalise it.

    Callers must have checked that the new canonical text does not collide
    with another composition — two rows for one formulation would split a set
    of alternatives in half.
    """
    canonical = canonical_text(components)
    composition.composition_text = canonical
    composition.search_key = composition_search_key(canonical)

    composition.components.clear()
    db.flush()
    for name, strength in components:
        salt = get_or_create_salt(db, name, actor_id)
        composition.components.append(
            CompositionSalt(composition_id=composition.id, salt_id=salt.id, strength=strength)
        )
    db.flush()


def get_or_create_salt(db: Session, name: str, actor_id: UUID | None = None) -> Salt:
    normalised = normalise_name(name)
    salt = db.query(Salt).filter(Salt.name == normalised).one_or_none()
    if salt is None:
        salt = Salt(name=normalised, created_by_id=actor_id, updated_by_id=actor_id)
        db.add(salt)
        db.flush()
    return salt


def resolve_composition(
    db: Session,
    *,
    composition_id: UUID | None = None,
    composition_text: str | None = None,
    components: list[tuple[str, str | None]] | None = None,
    therapeutic_class: str | None = None,
    actor_id: UUID | None = None,
) -> Composition:
    """Find (or build) the composition a medicine belongs to.

    Accepts an id, a free-text string, or parsed components — the CSV import
    and the manual form both funnel through here so they can never produce
    two rows for the same formulation.
    """
    if composition_id is not None:
        composition = db.get(Composition, composition_id)
        if composition is None:
            raise CompositionError(
                "Composition not found.", code="NOT_FOUND", field="composition_id", status_code=404
            )
        return composition

    if components is None:
        if not composition_text:
            raise CompositionError(
                "Provide either composition_id or a composition.", field="composition"
            )
        components = parse_composition_text(composition_text)

    if not components:
        raise CompositionError("Composition cannot be blank.", field="composition")

    canonical = canonical_text(components)
    existing = (
        db.query(Composition).filter(Composition.composition_text == canonical).one_or_none()
    )
    if existing is not None:
        if therapeutic_class and not existing.therapeutic_class:
            existing.therapeutic_class = therapeutic_class
        return existing

    composition = Composition(
        composition_text=canonical,
        search_key=composition_search_key(canonical),
        therapeutic_class=therapeutic_class,
        created_by_id=actor_id,
        updated_by_id=actor_id,
    )
    db.add(composition)
    db.flush()

    for name, strength in components:
        salt = get_or_create_salt(db, name, actor_id)
        db.add(
            CompositionSalt(
                composition_id=composition.id, salt_id=salt.id, strength=strength
            )
        )
    db.flush()
    return composition


def build_search_text(
    medicine: Medicine, composition: Composition | None, manufacturer: Manufacturer
) -> str:
    """One lowercase haystack per product: brand, formulation, every salt
    name, and the maker. This is what makes a brand *or* generic search work
    from a single LIKE.

    A general item has no formulation and no salts, so its haystack is just
    the brand, the maker and the pack — which is all anyone would type
    looking for a bar of soap.
    """
    salt_names = (
        " ".join(component.salt.name for component in composition.components)
        if composition is not None
        else ""
    )
    parts = [
        medicine.brand_name or "",
        (composition.composition_text if composition is not None else "") or "",
        salt_names,
        manufacturer.name or "",
        medicine.pack_label or "",
    ]
    return _WHITESPACE_RE.sub(" ", " ".join(parts)).strip().lower()[:600]


def refresh_search_text(db: Session, medicine: Medicine) -> None:
    composition = medicine.composition
    if composition is None and medicine.composition_id is not None:
        composition = db.get(Composition, medicine.composition_id)
    manufacturer = medicine.manufacturer or db.get(Manufacturer, medicine.manufacturer_id)
    medicine.search_text = build_search_text(medicine, composition, manufacturer)


def refresh_search_text_for_composition(db: Session, composition_id: UUID) -> int:
    """Renaming a composition must not strand medicines with stale haystacks."""
    affected = db.query(Medicine).filter(Medicine.composition_id == composition_id).all()
    for medicine in affected:
        refresh_search_text(db, medicine)
    return len(affected)


def refresh_search_text_for_manufacturer(db: Session, manufacturer_id: UUID) -> int:
    affected = db.query(Medicine).filter(Medicine.manufacturer_id == manufacturer_id).all()
    for medicine in affected:
        refresh_search_text(db, medicine)
    return len(affected)


class PackError(AppError):
    code = "PACK_INVALID"


# How each pack unit reads in a generated label.
_PACK_LABEL_UNIT = {
    PackUnit.TABLET: "tab",
    PackUnit.CAPSULE: "cap",
    PackUnit.ML: "ml",
    PackUnit.GM: "g",
    PackUnit.IU: "IU",
    PackUnit.VIAL: "vial",
    PackUnit.UNIT: "unit",
    PackUnit.PIECE: "pc",
}

_PACK_LABEL_CONTAINER = {
    PackUnit.TABLET: "strip",
    PackUnit.CAPSULE: "strip",
    PackUnit.ML: "bottle",
    PackUnit.GM: "pack",
    PackUnit.VIAL: "",
    PackUnit.IU: "",
    PackUnit.UNIT: "",
    PackUnit.PIECE: "pack",
}


def _trim(value: Decimal) -> str:
    """3.000 -> "3"; 21.800 -> "21.8"."""
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.to_integral_value()
    return f"{normalised:f}"


def build_pack_label(pack_size: Decimal, pack_unit: PackUnit) -> str:
    unit_word = _PACK_LABEL_UNIT.get(pack_unit, "unit")
    container = _PACK_LABEL_CONTAINER.get(pack_unit, "")
    if pack_unit is PackUnit.VIAL:
        size = _trim(pack_size)
        return "1 vial" if pack_size == 1 else f"{size} vials"
    label = f"{_trim(pack_size)} {unit_word}"
    return f"{label} {container}".strip()


def resolve_pack(
    dosage_form: str,
    pack_size: Decimal | None,
    pack_unit: str | None,
    pack_label: str | None = None,
) -> tuple[Decimal, str, int, str]:
    """Work out (pack_size, pack_unit, units_per_pack, pack_label) for a form.

    The rule the counter cares about: only tablets and capsules come in
    countable strips, so only they get a units-per-pack above 1. A 100 ml
    syrup is one sellable unit measured in ml, not "100 units" — otherwise
    the POS would happily try to sell a single millilitre.

    Returns the four values ready to assign to a Medicine.
    """
    form = DosageForm(dosage_form) if not isinstance(dosage_form, DosageForm) else dosage_form
    allowed = PACK_UNITS_BY_FORM.get(form, [PackUnit.UNIT])

    if pack_unit:
        try:
            unit = PackUnit(pack_unit)
        except ValueError:
            raise PackError(f"Unknown pack unit {pack_unit!r}.", field="pack_unit") from None
        if unit not in allowed:
            allowed_text = ", ".join(u.value for u in allowed)
            raise PackError(
                f"A {form.value.lower()} is measured in {allowed_text}, not {unit.value}.",
                field="pack_unit",
            )
    else:
        unit = default_pack_unit(form)

    size = Decimal(pack_size) if pack_size is not None else Decimal("1")
    if size <= 0:
        raise PackError("Pack size must be greater than zero.", field="pack_size")

    if form in COUNTABLE_FORMS and unit in COUNTABLE_UNITS:
        # A pack of countable articles holds a whole number of them, and each
        # one is sellable — this is what makes a single-tablet sale possible,
        # and equally a single soap out of a box of twelve. A general item
        # measured in ml or grams falls through: one bottle, one unit.
        if size != size.to_integral_value():
            raise PackError(
                f"A pack holds a whole number of {unit.value.lower()}s.", field="pack_size"
            )
        units_per_pack = int(size)
    else:
        units_per_pack = 1

    label = (pack_label or "").strip() or build_pack_label(size, unit)
    return size, unit.value, units_per_pack, label[:60]


# Pricing now lives with the stock it belongs to: see
# services.inventory.unit_price_from_pack and effective_discounts, which
# resolve against a batch rather than the medicine master.
