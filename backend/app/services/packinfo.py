"""Reading the pack description a price list actually carries.

Distributor and portal exports describe a pack in one free-text column —
"strip of 10 tablets", "bottle of 30 ml Oral Suspension", "vial of 1 Powder
for Injection". This turns that phrase into the three things the catalogue
stores: dosage form, pack size and the unit the pack is measured in.

The parse is deliberately conservative. Where the phrase does not say
something, nothing is invented: an unrecognised form comes back as OTHER
rather than being guessed at, because a wrong dosage form silently changes
whether the medicine is countable — and therefore whether the POS will let
someone sell a single piece of it.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from app.core.exceptions import AppError
from app.models.enums import DosageForm, PackUnit


class PackInfoError(AppError):
    code = "PACK_INFO_UNREADABLE"


class ParsedPack(NamedTuple):
    """What a pack phrase turned out to mean."""

    dosage_form: DosageForm
    pack_size: Decimal
    pack_unit: PackUnit | None
    #: The shop's own label, keeping the container the source named
    #: ("20 g tube" rather than the generic "20 g pack"). ``None`` leaves it
    #: to services.catalog.build_pack_label.
    pack_label: str | None


# "strip of 10 tablets", "bottle of 30 ml oral suspension", "1 tube of 20 gm"
_SHAPE = re.compile(
    r"""^\s*
    (?:\d+\s*)?                       # a leading "1 " on "1 tube of 20 gm"
    (?P<container>[a-z\s]+?)          # strip, bottle, vial, tube, prefilled syringe
    \s+of\s+
    (?P<qty>\d+(?:\.\d+)?)            # 10, 0.5, 100
    \s*
    (?P<rest>.*?)                     # "ml oral suspension", "tablets", ""
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# A phrase with no container: "10 tablets", "30 ml syrup".
_BARE = re.compile(
    r"^\s*(?P<qty>\d+(?:\.\d+)?)\s*(?P<rest>.*?)\s*$", re.IGNORECASE
)

# Measure words, in the unit they map to. Order matters only in that the
# longest match is taken first, so "gm" beats "g".
_MEASURES: list[tuple[str, PackUnit]] = [
    ("ml", PackUnit.ML),
    ("millilitre", PackUnit.ML),
    ("milliliter", PackUnit.ML),
    ("litre", PackUnit.ML),
    ("liter", PackUnit.ML),
    ("l", PackUnit.ML),
    ("gm", PackUnit.GM),
    ("gms", PackUnit.GM),
    ("gram", PackUnit.GM),
    ("grams", PackUnit.GM),
    ("g", PackUnit.GM),
    ("kg", PackUnit.GM),
    ("mg", PackUnit.GM),
    ("iu", PackUnit.IU),
]
# Litres and kilos are stated in the pack's own unit; the multiplier keeps
# "bottle of 1 litre" from becoming a one-millilitre bottle.
_MEASURE_SCALE = {"litre": 1000, "liter": 1000, "l": 1000, "kg": 1000}

# Form words -> dosage form. Longest phrase wins, so "powder for injection"
# is read as an injection rather than as a powder.
_FORMS: list[tuple[str, DosageForm]] = [
    ("powder for injection", DosageForm.INJECTION),
    ("solution for injection", DosageForm.INJECTION),
    ("injection", DosageForm.INJECTION),
    ("infusion", DosageForm.INJECTION),
    ("vaccine", DosageForm.INJECTION),
    ("prefilled syringe", DosageForm.INJECTION),
    ("oral suspension", DosageForm.SYRUP),
    ("oral solution", DosageForm.SYRUP),
    ("oral liquid", DosageForm.SYRUP),
    ("suspension", DosageForm.SYRUP),
    ("syrup", DosageForm.SYRUP),
    ("elixir", DosageForm.SYRUP),
    ("linctus", DosageForm.SYRUP),
    ("oral drops", DosageForm.DROPS),
    ("eye drops", DosageForm.DROPS),
    ("ear drops", DosageForm.DROPS),
    ("eye/ear drops", DosageForm.DROPS),
    ("nasal drops", DosageForm.DROPS),
    ("nasal spray", DosageForm.DROPS),
    ("drops", DosageForm.DROPS),
    ("ointment", DosageForm.OINTMENT),
    ("cream", DosageForm.OINTMENT),
    ("gel", DosageForm.OINTMENT),
    ("lotion", DosageForm.OINTMENT),
    ("balm", DosageForm.OINTMENT),
    ("paste", DosageForm.OINTMENT),
    ("soap", DosageForm.OINTMENT),
    ("powder", DosageForm.POWDER),
    ("granules", DosageForm.POWDER),
    ("sachet", DosageForm.POWDER),
    ("capsule", DosageForm.CAPSULE),
    ("capsules", DosageForm.CAPSULE),
    ("rotacap", DosageForm.CAPSULE),
    ("tablet", DosageForm.TABLET),
    ("tablets", DosageForm.TABLET),
    ("tab", DosageForm.TABLET),
    ("tabs", DosageForm.TABLET),
    ("caplet", DosageForm.TABLET),
]

# When the phrase names no form, the container is the next best evidence.
_CONTAINER_FORMS: dict[str, DosageForm] = {
    "strip": DosageForm.TABLET,
    "vial": DosageForm.INJECTION,
    "ampoule": DosageForm.INJECTION,
    "ampule": DosageForm.INJECTION,
    "prefilled syringe": DosageForm.INJECTION,
    "tube": DosageForm.OINTMENT,
    "sachet": DosageForm.POWDER,
}

# A container that fixes how the pack is measured whatever the form says: a
# vial of injection is one vial, not one millilitre.
_CONTAINER_UNITS: dict[str, PackUnit] = {
    "vial": PackUnit.VIAL,
    "ampoule": PackUnit.VIAL,
    "ampule": PackUnit.VIAL,
}

_WORD = re.compile(r"[a-z]+")

# Containers worth keeping in the shop's own label. Anything else falls back
# to the generic word for the unit.
_LABEL_CONTAINERS = {
    "strip", "bottle", "vial", "tube", "sachet", "packet", "jar", "box",
    "ampoule", "ampule", "tin", "can", "carton", "pack", "syringe",
    "prefilled syringe", "kit", "bag",
}

_LABEL_UNIT_WORD = {
    PackUnit.TABLET: "tab",
    PackUnit.CAPSULE: "cap",
    PackUnit.ML: "ml",
    PackUnit.GM: "g",
    PackUnit.IU: "IU",
    PackUnit.VIAL: "vial",
    PackUnit.UNIT: "unit",
}


def _trim(value: Decimal) -> str:
    normalised = value.normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.to_integral_value()
    return f"{normalised:f}"


def _label(size: Decimal, unit: PackUnit | None, container: str) -> str | None:
    """Keep the container the source named, in the shop's own format."""
    if not container or container not in _LABEL_CONTAINERS or unit is None:
        return None
    if unit is PackUnit.VIAL and container in ("vial", "ampoule", "ampule"):
        return None                      # catalog already writes "1 vial"
    return f"{_trim(size)} {_LABEL_UNIT_WORD.get(unit, 'unit')} {container}"[:60]


def _measure(rest: str) -> tuple[PackUnit | None, int, str]:
    """Split a leading measure word off "30 ml oral suspension"."""
    for word, unit in sorted(_MEASURES, key=lambda pair: -len(pair[0])):
        match = re.match(rf"{word}s?\b\.?\s*", rest, re.IGNORECASE)
        if match:
            return unit, _MEASURE_SCALE.get(word.lower(), 1), rest[match.end():].strip()
    return None, 1, rest


def _form(text: str) -> DosageForm | None:
    haystack = " ".join(_WORD.findall(text.lower()))
    for phrase, form in _FORMS:
        if re.search(rf"\b{re.escape(phrase)}\b", haystack):
            return form
    return None


def parse_pack_info(text: str) -> ParsedPack:
    """"strip of 10 tablets" -> (TABLET, 10, TABLET, "10 tab strip").

    Returns the dosage form, the pack size, the unit the size is measured in
    (``None`` where the phrase does not say and the form's default should
    stand) and a label. Raises :class:`PackInfoError` when there is no
    quantity to read, because a pack with no size is not a pack.
    """
    raw = (text or "").strip()
    if not raw:
        raise PackInfoError("Pack info is empty.", field="pack_info")

    cleaned = re.sub(r"\s+", " ", raw.lower().replace("–", "-"))
    match = _SHAPE.match(cleaned)
    container = ""
    if match:
        container = match.group("container").strip()
    else:
        match = _BARE.match(cleaned)
    if not match:
        raise PackInfoError(
            f"“{raw}” does not say how much is in the pack.", field="pack_info"
        )

    try:
        size = Decimal(match.group("qty"))
    except InvalidOperation:
        raise PackInfoError(f"“{raw}” has no readable quantity.", field="pack_info") from None
    if size <= 0:
        raise PackInfoError("Pack size must be greater than zero.", field="pack_info")

    unit, scale, remainder = _measure(match.group("rest"))
    size *= scale

    form = _form(remainder) or _form(container) or _CONTAINER_FORMS.get(container)
    if form is None:
        # Nothing in the phrase names a form. OTHER is measured in whatever
        # the phrase did say, which is better than assuming "tablet" and
        # making the item sellable a piece at a time.
        form = DosageForm.OTHER

    # A vial is counted as a vial even when the phrase gives its volume:
    # "vial of 2 ml injection" is one sellable vial, not two millilitres.
    if container in _CONTAINER_UNITS and size == 1:
        unit = _CONTAINER_UNITS[container]

    if form in (DosageForm.TABLET, DosageForm.CAPSULE):
        # A strip holds whole pieces; "strip of 10 tablets" is measured in
        # tablets whatever else the phrase mentioned.
        unit = PackUnit.TABLET if form is DosageForm.TABLET else PackUnit.CAPSULE
        if size != size.to_integral_value():
            raise PackInfoError(
                f"“{raw}” reads as {size} {form.value.lower()}s — a strip holds whole pieces.",
                field="pack_info",
            )

    return ParsedPack(form, size, unit, _label(size, unit, container))
