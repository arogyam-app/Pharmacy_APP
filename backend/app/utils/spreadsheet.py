"""Reading and writing the spreadsheets people actually have.

A shop's medicine list arrives as whatever the distributor sent — usually an
Excel file, sometimes a CSV exported from the old billing package. Both are
read here so an importer never has to care which it was given, and the
download templates are produced from the same column definitions the
importer validates against: the header a user downloads is by construction
the header the importer expects.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import NamedTuple

from fastapi import Response

from app.core.exceptions import AppError
from app.utils.csv_export import csv_response

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_SUFFIXES = (".xlsx", ".xlsm")

# Shared by every importer.
#
# These are sized for the real thing rather than a sample: the Indian drug
# formulary runs to tens of thousands of distinct formulations, and a shop
# loading its master data starts with the whole list, not a tenth of it. The
# old 5,000-row ceiling turned a one-off load into four uploads and a tally of
# which rows had already gone in.
#
# The cap is not arbitrary. The file is read into memory whole and each row
# costs a lookup and a savepoint — measured at ~130 rows/second — so 50,000
# rows is about six minutes of work and roughly 20 MB of text. Past that it
# really is a data migration, and `psql \copy` is the right tool.
#
# Anything raising these must also raise the client timeout in
# `frontend/src/api/client.js`, or the browser gives up long before the server
# finishes and the operator sees a network error over a working import.
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_IMPORT_ROWS = 50_000


def check_size(raw: bytes) -> None:
    if len(raw) > MAX_IMPORT_BYTES:
        limit_mb = MAX_IMPORT_BYTES // (1024 * 1024)
        raise AppError(
            f"File is larger than {limit_mb} MB.", code="FILE_TOO_LARGE", field="file"
        )


class Column(NamedTuple):
    """One template column: the header written out and the help beside it."""

    name: str
    required: bool
    help: str
    example: str = ""


class Row(NamedTuple):
    """One data row, carrying the line number a person would see on screen."""

    number: int
    values: dict[str, str]

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    @property
    def is_blank(self) -> bool:
        return not any(self.values.values())


def _cell(value) -> str:
    """Spreadsheet cell -> the string a CSV would have carried.

    Excel stores a typed number, so a pack size keyed as 15 arrives as
    ``15.0``. Writing that straight through would make ``500mg`` into
    ``500.0mg`` further down the line.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, Decimal, float)):
        return str(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def normalise(header) -> str:
    """"Price (INR)" -> "price_inr".

    Header rows in the wild carry spaces, slashes and units in brackets.
    Everything that is not a letter or a digit becomes a single underscore,
    so a column can be matched by name however it was punctuated.
    """
    text = str(header or "").strip().lower()
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text)).strip("_")


_normalise = normalise   # the private name the module used first


def read_table(
    filename: str | None, raw: bytes, *, required: Sequence[str] = ()
) -> tuple[list[str], list[Row]]:
    """Read a CSV or XLSX upload into headers and rows.

    Row numbers are the ones the spreadsheet shows, so "row 14 is wrong"
    sends someone to the right line rather than to an off-by-one argument.

    `required` names columns that identify the header row — the first row
    carrying **any** of them wins, which is how a sheet with a title line
    above the table still reads correctly.
    """
    if (filename or "").lower().endswith(XLSX_SUFFIXES):
        return _read_xlsx(raw, required)
    return _read_csv(raw)


def _read_csv(raw: bytes) -> tuple[list[str], list[Row]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AppError(
            "File must be UTF-8 encoded, or saved as .xlsx.",
            code="FILE_NOT_UTF8",
            field="file",
        ) from None

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise AppError("The file has no header row.", code="FILE_EMPTY", field="file")

    headers = [_normalise(h) for h in reader.fieldnames]
    rows = [
        # Row 1 is the header, so data starts at 2.
        Row(index, {_normalise(k): _cell(v) for k, v in raw_row.items() if k is not None})
        for index, raw_row in enumerate(reader, start=2)
    ]
    return headers, rows


def _read_xlsx(raw: bytes, required: Sequence[str] = ()) -> tuple[list[str], list[Row]]:
    from openpyxl import load_workbook

    try:
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises a zoo of errors
        raise AppError(
            f"That file could not be read as a spreadsheet ({exc}).",
            code="FILE_UNREADABLE",
            field="file",
        ) from None

    sheet = book.worksheets[0]
    grid = list(sheet.iter_rows(values_only=True))
    book.close()

    # Skip anything above the header — a title line above the table is
    # common in files people forward from a distributor. The header is the
    # first row carrying the columns the importer needs; failing that, the
    # first row with anything in it, so the error names a real header.
    # Any one of the names is enough: an importer that accepts either
    # "brand_name" or "Medicine Name" will never see both in one file.
    wanted = {_normalise(name) for name in required}

    def carries_headers(row) -> bool:
        return bool(wanted & {_normalise(cell) for cell in row if cell is not None})

    start = next((i for i, row in enumerate(grid) if wanted and carries_headers(row)), None)
    if start is None:
        start = next(
            (i for i, row in enumerate(grid) if sum(c is not None for c in row) > 0), None
        )
    if start is None:
        raise AppError("The sheet is empty.", code="FILE_EMPTY", field="file")

    headers = [_normalise(h) for h in grid[start]]
    rows = [
        Row(start + offset + 2, {h: _cell(v) for h, v in zip(headers, values) if h})
        for offset, values in enumerate(grid[start + 1 :])
    ]
    return headers, rows


def missing_columns(headers: Iterable[str], columns: Sequence[Column]) -> list[str]:
    present = set(headers)
    return sorted(c.name for c in columns if c.required and c.name not in present)


# --- templates -----------------------------------------------------------


def csv_template(columns: Sequence[Column], *, filename: str):
    """Header row only.

    Deliberately no sample data: a template that arrives pre-filled gets
    uploaded unchanged, and then someone has to go and delete the examples
    back out of the live master.
    """
    return csv_response(filename=filename, header=[c.name for c in columns], rows=[])


def xlsx_template(columns: Sequence[Column], *, title: str, guidance: Sequence[str] = ()) -> bytes:
    """The same header, plus a sheet explaining each column.

    The instructions live on their own sheet rather than above the table, so
    the first sheet is exactly what the importer reads.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = title[:31]

    header_font = Font(bold=True, color="FFFFFF")
    required_fill = PatternFill("solid", fgColor="0F766E")
    optional_fill = PatternFill("solid", fgColor="5F6B7A")

    for index, column in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=index, value=column.name)
        cell.font = header_font
        cell.fill = required_fill if column.required else optional_fill
        cell.alignment = Alignment(horizontal="left")
        sheet.column_dimensions[get_column_letter(index)].width = max(16, len(column.name) + 6)

    sheet.freeze_panes = "A2"

    notes = book.create_sheet("How to fill this in")
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 14
    notes.column_dimensions["C"].width = 70
    notes.column_dimensions["D"].width = 46

    for index, heading in enumerate(("Column", "Required?", "What it means", "Example"), start=1):
        cell = notes.cell(row=1, column=index, value=heading)
        cell.font = Font(bold=True)

    line = 2
    for column in columns:
        notes.cell(row=line, column=1, value=column.name)
        notes.cell(row=line, column=2, value="Required" if column.required else "Optional")
        notes.cell(row=line, column=3, value=column.help).alignment = Alignment(wrap_text=True)
        notes.cell(row=line, column=4, value=column.example)
        line += 1

    for note in guidance:
        line += 1
        notes.cell(row=line, column=1, value=note).alignment = Alignment(wrap_text=True)
        notes.merge_cells(start_row=line, start_column=1, end_row=line, end_column=4)

    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def xlsx_table(
    columns: Sequence[tuple[str, str, str]],
    rows: Sequence[dict],
    *,
    title: str,
    subtitle: str = "",
    totals: dict | None = None,
    numeric_types: Sequence[str] = (),
) -> bytes:
    """A report as a spreadsheet: title, header, rows, bold totals.

    `columns` are `(key, label, type)` triples. Values arrive as strings — the
    API's exact-decimal convention — and any column whose type is in
    `numeric_types` is written back as a real number so the person who opens
    the file can sum, sort and pivot it. A value that will not parse is
    written as text rather than dropped.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    # Excel forbids : \ / ? * [ ] in a sheet name and caps it at 31 chars.
    sheet.title = re.sub(r"[:\\/?*\[\]]", " ", title)[:31]

    sheet["A1"] = title
    sheet["A1"].font = Font(bold=True, size=14)
    if subtitle:
        sheet["A2"] = subtitle
        sheet["A2"].font = Font(italic=True, color="5F6B7A")

    header_row = 4 if subtitle else 3
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0F766E")

    for index, (_, label, _type) in enumerate(columns, start=1):
        cell = sheet.cell(row=header_row, column=index, value=label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left")
        sheet.column_dimensions[get_column_letter(index)].width = max(14, len(label) + 4)

    def write(line: int, values: dict, bold: bool = False) -> None:
        for index, (key, _label, kind) in enumerate(columns, start=1):
            cell = sheet.cell(row=line, column=index)
            cell.value = _typed(values.get(key), kind in numeric_types)
            if kind in numeric_types and isinstance(cell.value, (int, float, Decimal)):
                cell.number_format = "#,##0" if kind == "int" else "#,##0.00"
            if bold:
                cell.font = Font(bold=True)

    line = header_row
    for row in rows:
        line += 1
        write(line, row)

    if totals:
        line += 1
        write(line, totals, bold=True)
        # Name the row, so a printed sheet does not end in an unlabelled
        # line of numbers. The first column is whatever the report groups by.
        first = sheet.cell(row=line, column=1)
        if first.value in (None, ""):
            first.value = "Total"
            first.font = Font(bold=True)

    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def _typed(value, numeric: bool):
    """A cell value: a real number where the column says so, else text."""
    if value is None or value == "":
        return None
    if not numeric:
        return value
    try:
        number = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return value
    # openpyxl writes Decimal as a string; float is the only numeric type it
    # stores, and at report scale (< 2^53 paise) it is exact.
    return int(number) if number == number.to_integral_value() else float(number)


def xlsx_response(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type=XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
