"""Report primitives: one table shape, rendered three ways.

Every report in Phase 7 answers the same question in a different way — *what
happened between these two dates* — so they all produce the same structure: a
list of typed columns and a list of rows keyed by those columns, plus an
optional totals row. That uniformity is the point:

* the router renders **one** shape as JSON, CSV or XLSX, so `?format=` is
  implemented once rather than thirteen times;
* the SPA has one table component driven by `columns`, so adding a report
  later is a backend change only.

**Numbers leave here as strings**, at the scale of their column — `"1234.50"`,
never `1234.5`. That is what the rest of the API already does with money
(`Decimal` serialises to a string), and it keeps paise exact all the way to
the spreadsheet. The XLSX writer turns them back into real numbers using the
column type, so a shopkeeper can still sum a column in Excel.

**A caution that shapes every query in this package:** on SQLite these decimal
columns are stored as scaled integers (see `app/db/types.py`). `SUM(col)` is
safe — SQLAlchemy carries the column type onto the sum and unscales the
result. `col_a * col_b` is *not*: two scaled integers multiply to a number
scaled twice over. Any product — quantity x cost, quantity x MRP — is computed
in Python, never in SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, NamedTuple

from app.db.types import quantize_money, quantize_qty

ZERO = Decimal("0")


class ColumnType:
    """How a value is formatted, aligned and summed. Mirrored in the SPA."""

    TEXT = "text"
    INT = "int"
    MONEY = "money"
    QTY = "qty"
    RATE = "rate"
    DATE = "date"
    DATETIME = "datetime"


#: Column types the XLSX writer converts back to real numbers.
NUMERIC_TYPES = {ColumnType.INT, ColumnType.MONEY, ColumnType.QTY, ColumnType.RATE}


class Column(NamedTuple):
    key: str
    label: str
    type: str = ColumnType.TEXT


@dataclass
class ReportTable:
    """One rendered report, before it becomes JSON, CSV or a spreadsheet."""

    key: str
    title: str
    columns: list[Column]
    rows: list[dict[str, Any]]
    #: Same keys as a row. The column a total makes no sense for is simply
    #: absent — a summed GST *rate* would be a nonsense number.
    totals: dict[str, Any] | None = None
    #: Anything the report wants to say that is not a row: the store's GSTIN,
    #: which stock the valuation was taken at, why a figure is missing.
    meta: dict[str, Any] = field(default_factory=dict)


# --- value formatting ----------------------------------------------------
#
# One function per column type, so a report never hand-formats a number and
# the CSV, the JSON and the spreadsheet can never disagree about scale.


def money(value: Decimal | int | None) -> str:
    return str(quantize_money(Decimal(value or 0)))


def qty(value: Decimal | int | None) -> str:
    return str(quantize_qty(Decimal(value or 0)))


def rate(value: Decimal | int | None) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def pct(part: Decimal | int | None, whole: Decimal | int | None) -> str:
    """A percentage of one figure by another, or "0.00" if there is no whole.

    Margin on a zero-value sale is not 0 % and not infinite — it is a
    question that was not asked. Reporting 0.00 keeps the column summable
    and is the convention the rest of the app already uses for empty money.
    """
    whole = Decimal(whole or 0)
    if whole == 0:
        return "0.00"
    return rate(Decimal(part or 0) * 100 / whole)


def total_of(rows: Iterable[dict[str, Any]], key: str, formatter=money) -> str:
    """Sum one already-formatted column back up for the totals row."""
    return formatter(sum((Decimal(row[key]) for row in rows if row.get(key) is not None), ZERO))


def sum_columns(
    rows: list[dict[str, Any]], money_keys: Iterable[str] = (), int_keys: Iterable[str] = (),
    qty_keys: Iterable[str] = (),
) -> dict[str, Any]:
    """The usual totals row: money summed to paise, counts summed as ints."""
    totals: dict[str, Any] = {key: total_of(rows, key, money) for key in money_keys}
    totals.update({key: total_of(rows, key, qty) for key in qty_keys})
    totals.update({key: sum(int(row.get(key) or 0) for row in rows) for key in int_keys})
    return totals
