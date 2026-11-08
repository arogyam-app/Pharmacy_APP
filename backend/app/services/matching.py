"""Finding a product from the way a supplier's invoice writes its name.

A distributor does not write what the shop's master holds. `P 120 Syruop`
(a typo) should still find every `P 120 Syrup`, and `T Minic Drops Af`
should find `T Minic Drops` even though nothing on the master is named
"T Minic Drops Af". Two ideas do most of the work:

* **Match on a shrinking prefix of the name, not the whole string.** The
  line is read left to right, the way the name on the master is: try
  `brand_name` starting with the line as typed, and if nothing does, drop
  the last word and try again, until something matches or one word is all
  that is left. This is deliberately **not** order-blind — a supplier's
  line reads the same direction the product's name does, so a match earns
  its place by sharing a prefix, not by sharing words in any order.

* **The pack size is part of the identity.** `Volini Gel 10GM` and
  `Volini Gel 30GM` are two products, not one written two ways. So a size on
  the invoice is pulled out of the text, matched against the candidate's own
  pack, and reported separately — a candidate whose *name* matches but whose
  *pack* does not is shown as exactly that, never silently accepted. A
  dosage-form word (`TAB`/`CAP`/`SYP`…) is read the same way, as a tie-break
  rather than a requirement.

Nothing here decides anything. It returns ranked candidates and says what it
noticed; choosing one, or adding a product that is genuinely not on the
master, is the operator's call. A distributor's abbreviation is not evidence
enough to bind money to a row.
"""

from uuid import UUID

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.catalog import Medicine

#: Words that describe the *form* rather than the product. Dropped from the
#: token set, because a supplier writes them inconsistently or not at all —
#: but only when another token survives, so a line reading just "TABLET"
#: still searches for something.
FORM_WORDS = frozenset(
    """
    tab tabs tablet tablets
    cap caps capsule capsules
    syp syr syrup susp suspension
    inj injection vial amp ampoule
    oint ointment
    sol soln solution drop drops
    pdr powder sachet sachets
    lot lotion spray kit
    """.split()
)

#: Noise a distributor adds that says nothing about which product it is.
STOP_WORDS = frozenset("of the and with plus new pack packs strip strips box".split())

#: The subset of FORM_WORDS that names a `DosageForm` unambiguously. Words
#: like "sol"/"solution" or "lotion"/"spray" describe a form too, but map to
#: no single DosageForm on the master, so they stay noise for `tokenise()`
#: without becoming a hint here — a hint that is wrong more often than it
#: is right would push the real row down the list instead of up it.
FORM_WORD_TO_DOSAGE_FORM = {
    "tab": "TABLET", "tabs": "TABLET", "tablet": "TABLET", "tablets": "TABLET",
    "cap": "CAPSULE", "caps": "CAPSULE", "capsule": "CAPSULE", "capsules": "CAPSULE",
    "syp": "SYRUP", "syr": "SYRUP", "syrup": "SYRUP",
    "susp": "SYRUP", "suspension": "SYRUP",
    "inj": "INJECTION", "injection": "INJECTION",
    "vial": "INJECTION", "amp": "INJECTION", "ampoule": "INJECTION",
    "oint": "OINTMENT", "ointment": "OINTMENT",
    "drop": "DROPS", "drops": "DROPS",
    "pdr": "POWDER", "powder": "POWDER", "sachet": "POWDER", "sachets": "POWDER",
}

#: "30GM", "10 GM", "100ML", "500MG", "15'S", "10S" — a quantity written
#: against a unit. `'S` and a bare trailing `S` are how a strip count is
#: printed on an Indian invoice ("15'S" = fifteen tablets).
#:
#: `TAB` and `CAP` are deliberately **not** counting units. "PARACETAMOL 500
#: TAB" is a 500 mg tablet, not a pack of five hundred, and reading it as a
#: pack count would tell the operator the product is missing from the master
#: and offer to add a 500-count pack. A real strip count is written with
#: `'S`, `S`, `NOS` or `PCS`.
_PACK_RE = re.compile(
    r"(?<![A-Z0-9])(?P<size>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>GM|GMS|G|KG|ML|LTR|L|MG|MCG|IU|'S|S|N|PCS|PC|NOS)"
    r"(?![A-Z])",
    re.IGNORECASE,
)

#: How a written unit maps onto a PackUnit. Weight and volume name the pack
#: measure directly; a count names how many countable articles are in it.
_UNIT_TO_PACK = {
    "GM": "GM", "GMS": "GM", "G": "GM",
    "ML": "ML", "LTR": "ML", "L": "ML",
    "IU": "IU",
    "'S": "COUNT", "S": "COUNT", "N": "COUNT", "NOS": "COUNT",
    "PCS": "COUNT", "PC": "COUNT",
}

_NON_WORD_RE = re.compile(r"[^A-Z0-9.]+")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class PackHint:
    """A size read off the invoice text, e.g. "30GM" or "15'S"."""

    text: str
    size: Decimal
    #: The PackUnit it implies, or "COUNT" when the number is a strip count
    #: (which is `units_per_pack`, not `pack_size`, for a countable pack).
    unit: str

    def matches(self, medicine: Medicine) -> bool:
        """Whether this medicine is that pack."""
        if self.unit == "COUNT":
            # "15'S" means fifteen sellable articles in the pack.
            return Decimal(medicine.units_per_pack or 1) == self.size
        if medicine.pack_unit != self.unit:
            return False
        try:
            return Decimal(medicine.pack_size) == self.size
        except (InvalidOperation, TypeError):
            return False


@dataclass
class FormHint:
    """A dosage form read off the invoice text, e.g. "TAB" -> TABLET."""

    text: str
    dosage_form: str

    def matches(self, medicine: Medicine) -> bool:
        return medicine.dosage_form == self.dosage_form


@dataclass
class Candidate:
    medicine: Medicine
    score: float
    #: False when the name matches but the pack does not — the Volini Gel
    #: 10GM / 30GM case, which is two products.
    pack_matches: bool = True
    #: False when the name matches but the invoice named a different form —
    #: a "PARACETAMOL SYP" should not rank a Paracetamol *tablet* above a
    #: Paracetamol *syrup*, even though both answer the word "paracetamol".
    form_matches: bool = True
    note: str | None = None


@dataclass
class MatchResult:
    query: str
    #: What was actually searched for, after the form words came off.
    terms: list[str] = field(default_factory=list)
    pack_hint: PackHint | None = None
    form_hint: FormHint | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def has_pack_match(self) -> bool:
        """True when some candidate is the pack the invoice asked for."""
        return any(c.pack_matches for c in self.candidates)

    @property
    def has_form_match(self) -> bool:
        """True when some candidate is the dosage form the invoice named."""
        return any(c.form_matches for c in self.candidates)


def normalise(text: str) -> str:
    """Uppercase, punctuation to spaces. `VOLINI-GEL 30GM` -> `VOLINI GEL 30GM`."""
    return _WHITESPACE_RE.sub(" ", _NON_WORD_RE.sub(" ", (text or "").upper())).strip()


def read_pack(text: str) -> PackHint | None:
    """Pull a pack size out of the invoice text, if it states one.

    The **last** one wins: a name may carry a strength early on
    (`AMOXY 500 CAP 10'S`) and the pack count at the end, and it is the pack
    that identifies which row on the master this is.
    """
    matches = list(_PACK_RE.finditer(normalise(text)))
    if not matches:
        return None
    for match in reversed(matches):
        unit = _UNIT_TO_PACK.get(match.group("unit").upper())
        if unit is None:
            continue
        try:
            size = Decimal(match.group("size"))
        except InvalidOperation:
            continue
        return PackHint(text=match.group(0).strip(), size=size, unit=unit)
    return None


def read_form(text: str) -> FormHint | None:
    """Pull a dosage form out of the invoice text, if it names one clearly.

    Unlike `tokenise()`, which drops "TAB"/"CAP"/"SYP" as noise so they never
    hide a product, this reads that same word *for* its meaning — so
    "PARACETAMOL SYP" can rank a Paracetamol syrup ahead of a Paracetamol
    tablet that otherwise matches the name equally well.
    """
    for word in normalise(text).split():
        form = FORM_WORD_TO_DOSAGE_FORM.get(word.lower())
        if form:
            return FormHint(text=word, dosage_form=form)
    return None


def tokenise(text: str) -> list[str]:
    """The words worth searching on.

    Form words and filler come out, because a supplier writes them however
    they like. If that would leave nothing, they go back in — a line that is
    *only* a form word still has to search for something.
    """
    words = [w for w in normalise(text).split() if len(w) > 1 or w.isdigit()]
    significant = [w for w in words if w.lower() not in FORM_WORDS | STOP_WORDS]
    return significant or words


def _score(medicine: Medicine, terms: list[str], hint: PackHint | None) -> float:
    """How well this row answers the invoice text. Higher is better.

    Deliberately simple and explainable: what fraction of the invoice's words
    the row accounts for, a bonus for the brand actually starting with the
    first of them, and a bonus for being the right pack. A score is only ever
    used to order the list the operator chooses from.
    """
    haystack = (medicine.search_text or "").lower()
    brand = (medicine.brand_name or "").lower()

    hit = sum(1 for term in terms if term.lower() in haystack)
    score = hit / max(len(terms), 1)

    if terms and brand.startswith(terms[0].lower()):
        score += 0.35
    if terms and brand == " ".join(terms).lower():
        score += 0.5
    if hint is not None and hint.matches(medicine):
        score += 0.4
    return score


def _progressive_prefix_rows(query, text: str, limit: int) -> tuple[list[Medicine], str]:
    """Match the invoice text against a product name, trimming one word off
    the end each time nothing answers.

    "P 120 Syruop" tries `brand_name` starting with "P 120 Syruop", then
    "P 120 Syrup" (a typo doesn't survive), then "P 120" — which is where
    every "P 120 Syrup" on the master starts, so that is what comes back.
    "T Minic Drops Af" tries the line whole, then "T Minic Drops", then
    "T Minic", then "T", stopping at the first prefix that matches anything.

    Case-insensitive throughout, and deliberately **not** the order-blind
    matching `tokenise()` feeds the rest of this module: a supplier's line
    is read left to right, the same way the name on the master is, and each
    step only ever narrows what was already typed rather than reordering
    it.
    """
    words = normalise(text).split()
    while words:
        prefix = " ".join(words)
        rows = (
            query.filter(func.lower(Medicine.brand_name).like(f"{prefix.lower()}%"))
            .order_by(Medicine.brand_name)
            .limit(limit * 4)
            .all()
        )
        if rows:
            return rows, prefix
        words.pop()
    return [], ""


def find(
    db: Session, *, store_id: UUID, text: str, limit: int = 12, item_type: str | None = None
) -> MatchResult:
    """Rank the products a line of a supplier's invoice might mean.

    Never picks one. The caller shows the list and the operator chooses, or
    says none of them is it — which is the honest answer often enough that
    guessing would be worse than asking.
    """
    result = MatchResult(
        query=text, terms=tokenise(text), pack_hint=read_pack(text), form_hint=read_form(text)
    )
    # Not `result.terms`: tokenise() drops a bare single letter as noise
    # for scoring purposes, but "P" alone is a real, if blunt, invoice line
    # -- every "P ..." brand is exactly what backing off to one word means.
    # Only truly empty input (blank, or punctuation alone) skips the search.
    if not normalise(text).split():
        return result

    query = db.query(Medicine).filter(
        Medicine.store_id == store_id, Medicine.is_active.is_(True)
    )
    if item_type is not None:
        query = query.filter(Medicine.item_type == item_type)

    rows, _matched_prefix = _progressive_prefix_rows(query, text, limit)

    scored = [
        Candidate(
            medicine=row,
            score=_score(row, result.terms, result.pack_hint),
            pack_matches=result.pack_hint is None or result.pack_hint.matches(row),
            form_matches=result.form_hint is None or result.form_hint.matches(row),
        )
        for row in rows
    ]
    for candidate in scored:
        if not candidate.pack_matches:
            candidate.note = (
                f"Same name, different pack — this one is {candidate.medicine.pack_label}."
            )
        elif not candidate.form_matches:
            candidate.note = (
                f"Same name, different form — this one is "
                f"{candidate.medicine.dosage_form.title()}."
            )

    def rank(candidate: Candidate) -> tuple:
        """The asked-for pack leads — a size mismatch is a different
        physical product, the one thing this matcher must never blur. Then
        the asked-for form (TAB/CAP/SYP…); then alphabetically — every
        candidate here already starts with the matched prefix by
        construction, so what is left to order is which of them the
        invoice's form word points at, then the brand name itself.
        """
        brand = (candidate.medicine.brand_name or "").lower()
        return (
            0 if candidate.pack_matches else 1,
            0 if candidate.form_matches else 1,
            brand,
        )

    scored.sort(key=rank)
    result.candidates = scored[:limit]
    return result
