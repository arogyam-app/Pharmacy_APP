"""Reading a supplier's item list out of a spreadsheet, for review.

A distributor sends the lines as a file. Parsing them is easy; deciding
*which product each line means* is not, because a supplier writes
`PARACETAMOL TAB`, `VOLINI GEL 30GM`, `AMOXY 500 CAP 10'S` and none of that
is what the master holds.

So this module stops short of receiving anything. It turns a file into rows
with their figures parsed and, for each one, the candidate products it might
mean — and hands the lot back for a person to resolve. Stock is created only
by the ordinary goods-inward call, once every row names a real product.

That split is the point. A file is a claim about what arrived, and the shop
is the only party that can confirm it.
"""

from uuid import UUID

import calendar
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.enums import ExpiryBasis
from app.services import matching
from app.utils.spreadsheet import Column, Row

#: The template a shop downloads and a distributor's list is pasted into.
#: Deliberately the same fields the manual form asks for, in the order the
#: form asks for them, so the two never disagree about what a line needs.
COLUMNS: list[Column] = [
    Column("product", True, "Name as the supplier wrote it — a trailing TAB or SYP is fine", "VOLINI GEL 30GM"),
    Column("batch_no", True, "Batch or lot number as printed", "B2291"),
    Column("expiry_date", False, "YYYY-MM-DD, or MM/YY off the pack (read as that month's last day). Leave blank if the pack shows a manufacturing date instead", "08/28"),
    Column("mfg_date", False, "YYYY-MM-DD, with best_before_months, when there is no printed expiry", ""),
    Column("best_before_months", False, "Months after the manufacturing date, e.g. 24", ""),
    Column("qty_packs", True, "Packs billed", "10"),
    Column("free_qty_packs", False, "Scheme goods — received but not billed", "1"),
    Column("mrp_per_pack", True, "Printed on the box, GST-inclusive", "150.00"),
    Column("rate_per_pack", True, "What the distributor charged, GST-exclusive", "100.00"),
    Column("discount_pct", False, "Trade discount on this line", "5"),
    Column("gst_rate", False, "Defaults to 12 if left blank", "12"),
    Column("default_discount_pct", False, "Retail discount to suggest at the counter", ""),
    Column("max_discount_pct", False, "Retail discount ceiling for this batch", ""),
]

GUIDANCE = (
    "One row per item on the supplier's invoice. The supplier's own details go on the "
    "screen, not in this file.",
    "product: write it exactly as the invoice does. A trailing TAB, TABS or SYP is "
    "ignored when matching, and a size such as 30GM is read out and checked — two "
    "sizes of one brand are two different products.",
    "Shelf life: give expiry_date, OR mfg_date with best_before_months, OR leave all "
    "three blank for goods that do not expire.",
    "Every row is shown for confirmation before any stock moves. Nothing in this file "
    "is accepted on its own.",
)

REQUIRED_HEADERS = ("product", "batch_no", "qty_packs")


@dataclass
class ParsedLine:
    """One row of the file: what it says, and what it might mean."""

    row: int
    product_text: str
    batch_no: str = ""
    expiry_basis: str = ExpiryBasis.EXPIRY_DATE.value
    expiry_date: date | None = None
    mfg_date: date | None = None
    best_before_months: int | None = None
    qty_packs: Decimal | None = None
    free_qty_packs: Decimal = Decimal("0")
    mrp_per_pack: Decimal | None = None
    cost_per_pack: Decimal | None = None
    discount_pct: Decimal = Decimal("0")
    gst_rate: Decimal = Decimal("12")
    default_discount_pct: Decimal | None = None
    max_discount_pct: Decimal | None = None
    #: Candidates for `product_text`, best first. Never resolved here.
    match: matching.MatchResult | None = None
    #: Why the row cannot be received as it stands.
    errors: list[str] = field(default_factory=list)


def _decimal(row: Row, key: str, *, default: Decimal | None = None) -> Decimal | None:
    raw = row.get(key).strip()
    if not raw:
        return default
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{key}: {raw!r} is not a number") from None


#: "08/26", "8/2026", "08-26" — a month/year printed on the pack, which is
#: how most batches actually state their expiry. No day is separator-free
#: ("0826") because that is indistinguishable from a two-digit day-month
#: typo; a supplier who means month/year writes a separator.
_MONTH_YEAR_RE = re.compile(r"^(?P<month>\d{1,2})[/\-](?P<year>\d{2}|\d{4})$")


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _date(row: Row, key: str) -> date | None:
    raw = row.get(key).strip()
    if not raw:
        return None
    # Spreadsheets hand back a date as "2028-03-31 00:00:00" often enough.
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        raise ValueError(f"{key}: {raw!r} is not a date (use YYYY-MM-DD)") from None


def _expiry_date(row: Row, key: str) -> date | None:
    """Like `_date`, but a pack's expiry is printed as a month more often
    than as a day — "EXP 08/26" — and the batch is good through the **last**
    day of that month, the standard reading of a month-only expiry.

    Not used for `mfg_date`: reading a manufacturing month as its *last* day
    would push a best-before shelf life later than the pack can support, the
    wrong direction to guess wrong in.
    """
    raw = row.get(key).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        pass

    match = _MONTH_YEAR_RE.match(raw)
    if match:
        month = int(match.group("month"))
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        if 1 <= month <= 12:
            return _last_day_of_month(year, month)

    raise ValueError(f"{key}: {raw!r} is not a date (use YYYY-MM-DD, or MM/YY for an expiry)")


def parse_row(row: Row) -> ParsedLine:
    """One spreadsheet row into figures, collecting every problem with it.

    Problems are collected rather than raised at the first one: an operator
    fixing a file wants to see all of a row's faults at once.
    """
    line = ParsedLine(row=row.number, product_text=row.get("product").strip())

    if not line.product_text:
        line.errors.append("product is blank")
    line.batch_no = row.get("batch_no").strip()
    if not line.batch_no:
        line.errors.append("batch_no is blank")

    for key, setter in (
        ("qty_packs", "qty_packs"),
        ("free_qty_packs", "free_qty_packs"),
        ("mrp_per_pack", "mrp_per_pack"),
        ("rate_per_pack", "cost_per_pack"),
        ("discount_pct", "discount_pct"),
        ("gst_rate", "gst_rate"),
        ("default_discount_pct", "default_discount_pct"),
        ("max_discount_pct", "max_discount_pct"),
    ):
        try:
            value = _decimal(row, key, default=getattr(line, setter))
        except ValueError as exc:
            line.errors.append(str(exc))
            continue
        setattr(line, setter, value)

    try:
        line.expiry_date = _expiry_date(row, "expiry_date")
    except ValueError as exc:
        line.errors.append(str(exc))
    try:
        line.mfg_date = _date(row, "mfg_date")
    except ValueError as exc:
        line.errors.append(str(exc))

    months = row.get("best_before_months").strip()
    if months:
        try:
            line.best_before_months = int(Decimal(months))
        except (InvalidOperation, ValueError):
            line.errors.append(f"best_before_months: {months!r} is not a whole number")

    # The three ways a pack states its shelf life, inferred from what the row
    # filled in — the same rule the manual form follows.
    if line.expiry_date is not None:
        line.expiry_basis = ExpiryBasis.EXPIRY_DATE.value
    elif line.mfg_date is not None and line.best_before_months is not None:
        line.expiry_basis = ExpiryBasis.BEST_BEFORE.value
    elif line.mfg_date is not None or line.best_before_months is not None:
        line.errors.append(
            "a best-before shelf life needs both mfg_date and best_before_months"
        )
    else:
        line.expiry_basis = ExpiryBasis.NONE.value

    if line.qty_packs is None or line.qty_packs <= 0:
        line.errors.append("qty_packs must be more than zero")
    if line.mrp_per_pack is None or line.mrp_per_pack <= 0:
        line.errors.append("mrp_per_pack must be more than zero")
    if line.cost_per_pack is None or line.cost_per_pack < 0:
        line.errors.append("rate_per_pack is required")

    return line


def review(
    db: Session, *, store_id: UUID, rows: list[Row], match_limit: int = 8
) -> list[ParsedLine]:
    """Parse every row and attach the products each one might mean.

    Deliberately returns candidates, never a choice. A supplier's wording is
    not evidence enough to bind money to a product, so each row comes back
    for a person to confirm — and a row whose pack size is not on the master
    comes back saying exactly that, because `Volini Gel 10GM` and
    `Volini Gel 30GM` are two products.
    """
    lines = []
    for row in rows:
        if row.is_blank:
            continue
        line = parse_row(row)
        if line.product_text:
            line.match = matching.find(
                db, store_id=store_id, text=line.product_text, limit=match_limit
            )
        lines.append(line)
    return lines
