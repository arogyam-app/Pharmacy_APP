"""CSV downloads.

The server streams rows rather than building a string in memory, so an
export of a whole year's data does not depend on how much of it fits in
RAM. Reports (Phase 7) use the same helper.
"""

import csv
import io
from collections.abc import Iterable
from typing import Any

from fastapi.responses import StreamingResponse

# Excel on a Windows machine reads a UTF-8 file as the local codepage unless
# it finds a byte-order mark, which mangles ₹ and any non-ASCII name.
BOM = "﻿"


def csv_rows(header: list[str], rows: Iterable[list[Any]]) -> Iterable[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    writer.writerow(header)
    yield BOM + buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


def csv_response(*, filename: str, header: list[str], rows: Iterable[list[Any]]):
    return StreamingResponse(
        csv_rows(header, rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
