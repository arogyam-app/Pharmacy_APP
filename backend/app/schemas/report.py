"""Report request parameters and the single response envelope.

Every report endpoint takes the same three query parameters — `date_from`,
`date_to`, `format` — and returns the same envelope. A new report adds rows
and columns, never a new shape.
"""

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import Depends, Query
from pydantic import BaseModel, Field

from app.core.exceptions import AppError
from app.utils.clock import business_date

ReportFormat = Literal["json", "csv", "xlsx"]

#: Two years of daily data is already a slow query and a spreadsheet nobody
#: opens; past that the answer is a database export, not a report.
MAX_RANGE_DAYS = 732


class ReportColumn(BaseModel):
    key: str
    label: str
    type: str


class ReportOut(BaseModel):
    """What every `/reports/...` endpoint returns in JSON mode."""

    report: str
    title: str
    date_from: date
    date_to: date
    #: UTC, carrying its offset (CLAUDE.md rule 9).
    generated_at: str
    columns: list[ReportColumn]
    rows: list[dict[str, Any]]
    totals: dict[str, Any] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class DateRange(BaseModel):
    date_from: date
    date_to: date

    @property
    def label(self) -> str:
        return f"{self.date_from.isoformat()} to {self.date_to.isoformat()}"

    def slug(self, name: str) -> str:
        """`gst-outward-2026-07-01-to-2026-07-31` — a filename that sorts."""
        return f"{name}-{self.date_from.isoformat()}-to-{self.date_to.isoformat()}"


def report_range(
    date_from: Annotated[date | None, Query(description="Defaults to the 1st of the month")] = None,
    date_to: Annotated[date | None, Query(description="Defaults to today")] = None,
) -> DateRange:
    """The date window, defaulting to the current month at the counter.

    "Today" is the business date in IST, not the UTC one: before 05:30 IST
    those are different days, and a day-book that quietly reported yesterday
    would be wrong exactly when the night shift is cashing up (rule 9).
    """
    end = date_to or business_date()
    start = date_from or end.replace(day=1)

    if start > end:
        raise AppError(
            "The start date is after the end date.",
            code="INVALID_DATE_RANGE",
            field="date_from",
        )
    if (end - start).days > MAX_RANGE_DAYS:
        raise AppError(
            f"Choose a range of {MAX_RANGE_DAYS} days or fewer.",
            code="DATE_RANGE_TOO_WIDE",
            field="date_from",
        )
    return DateRange(date_from=start, date_to=end)


ReportRange = Annotated[DateRange, Depends(report_range)]
