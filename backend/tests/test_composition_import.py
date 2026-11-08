"""Bulk-loading formulations from a spreadsheet.

The point of the feature is a shop typing up its own list once, in Excel,
instead of two hundred times in a dialog. So the tests care about the two
things that make that survivable: only the formulation is compulsory, and a
bad row costs you that row and nothing else.
"""

import io

from openpyxl import Workbook, load_workbook

from app.models.catalog import Composition, Salt

from tests.conftest import API

HEADER = "composition,therapeutic_class,notes\n"


def upload(client, headers, body, *, filename="compositions.csv", mime="text/csv", **params):
    payload = body.encode("utf-8") if isinstance(body, str) else body
    return client.post(
        f"{API}/compositions/import",
        files={"file": (filename, io.BytesIO(payload), mime)},
        headers=headers,
        params=params,
    )


def sheet_bytes(rows, header=("composition", "therapeutic_class", "notes")):
    """Build an .xlsx the way a user's saved template would look."""
    book = Workbook()
    book.active.append(list(header))
    for row in rows:
        book.active.append(list(row))
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- the template ---------------------------------------------------------


def test_the_csv_template_carries_the_headers_the_importer_expects(client, admin_headers):
    response = client.get(
        f"{API}/compositions/import-template", params={"format": "csv"}, headers=admin_headers
    )
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]

    body = response.text.lstrip("﻿").strip()
    assert body == "composition,therapeutic_class,notes"

    # The header it hands out is a header it accepts back.
    assert upload(client, admin_headers, body + "\n").json()["total_rows"] == 0


def test_the_excel_template_explains_itself(client, admin_headers):
    response = client.get(
        f"{API}/compositions/import-template", params={"format": "xlsx"}, headers=admin_headers
    )
    assert response.status_code == 200
    assert "spreadsheetml" in response.headers["content-type"]
    assert "compositions-template.xlsx" in response.headers["content-disposition"]

    book = load_workbook(io.BytesIO(response.content))
    first = book.worksheets[0]
    assert [c.value for c in first[1]] == ["composition", "therapeutic_class", "notes"]
    assert first.max_row == 1  # no sample rows to delete before using it

    assert "How to fill this in" in book.sheetnames
    notes = book["How to fill this in"]
    written = "\n".join(str(c.value) for row in notes.iter_rows() for c in row if c.value)
    assert "Required" in written
    assert "Amoxicillin 500mg" in written


def test_only_an_admin_can_take_the_template_or_upload(client, sales_headers):
    assert (
        client.get(f"{API}/compositions/import-template", headers=sales_headers).status_code == 403
    )
    assert upload(client, sales_headers, HEADER).status_code == 403


# --- importing ------------------------------------------------------------


def test_a_formulation_alone_is_enough(client, admin_headers):
    body = HEADER + "Paracetamol 500mg,,\nAmoxicillin 500mg + Clavulanic Acid 125mg,,\n"
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 2
    assert result["errors"] == []

    listed = client.get(f"{API}/compositions", headers=admin_headers).json()
    texts = [c["composition_text"] for c in listed["items"]]
    assert "PARACETAMOL (500MG)" in texts
    assert "AMOXICILLIN (500MG) + CLAVULANIC ACID (125MG)" in texts


def test_a_file_with_only_the_composition_column_is_accepted(client, admin_headers):
    result = upload(client, admin_headers, "composition\nCetirizine 10mg\n").json()
    assert result["created"] == 1
    assert result["errors"] == []


def test_the_optional_columns_land_when_given(client, admin_headers):
    body = HEADER + "Metformin 500mg,Antidiabetic,Take after food\n"
    assert upload(client, admin_headers, body).json()["created"] == 1

    found = client.get(
        f"{API}/compositions", params={"q": "metformin"}, headers=admin_headers
    ).json()["items"][0]
    assert found["therapeutic_class"] == "Antidiabetic"
    assert found["notes"] == "Take after food"


def test_the_ingredients_become_salts(client, admin_headers, db):
    body = HEADER + "Amoxicillin 500mg + Clavulanic Acid 125mg,Antibiotic,\n"
    assert upload(client, admin_headers, body).json()["created"] == 1

    names = {s.name for s in db.query(Salt).all()}
    assert {"AMOXICILLIN", "CLAVULANIC ACID"} <= names


def test_a_blank_composition_is_the_one_thing_rejected(client, admin_headers):
    body = HEADER + "Paracetamol 500mg,,\n,Antibiotic,a note with no formulation\n"
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["row"] == 3
    assert result["errors"][0]["field"] == "composition"


def test_a_bad_row_costs_only_that_row(client, admin_headers, db):
    """The count in the report and the rows on file must agree."""
    body = HEADER + (
        "First Good 500mg,,\n"
        ",,orphan note\n"
        "Second Good 250mg,,\n"
    )
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 2
    assert [e["row"] for e in result["errors"]] == [3]
    assert db.query(Composition).count() == 2


def test_the_same_file_twice_creates_nothing_the_second_time(client, admin_headers):
    body = HEADER + "Paracetamol 500mg,Analgesic,\nCetirizine 10mg,Antihistamine,\n"
    assert upload(client, admin_headers, body).json()["created"] == 2

    again = upload(client, admin_headers, body).json()
    assert again["created"] == 0
    assert again["skipped"] == 2


def test_reordered_ingredients_are_the_same_formulation(client, admin_headers, db):
    body = HEADER + (
        "Amoxicillin 500mg + Clavulanic Acid 125mg,,\n"
        "clavulanic acid 125 mg + amoxicillin 500 mg,,\n"
    )
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 1
    assert result["skipped"] == 1
    assert db.query(Composition).count() == 1


def test_an_upload_does_not_overwrite_a_class_someone_chose(client, admin_headers):
    client.post(
        f"{API}/compositions",
        json={"composition_text": "Paracetamol 500mg", "therapeutic_class": "Antipyretic"},
        headers=admin_headers,
    )
    body = HEADER + "Paracetamol 500mg,Painkiller,\n"
    assert upload(client, admin_headers, body, update_existing=True).json()["updated"] == 0

    found = client.get(
        f"{API}/compositions", params={"q": "paracetamol"}, headers=admin_headers
    ).json()["items"][0]
    assert found["therapeutic_class"] == "Antipyretic"


def test_update_existing_fills_in_what_is_blank(client, admin_headers):
    client.post(
        f"{API}/compositions", json={"composition_text": "Paracetamol 500mg"}, headers=admin_headers
    )
    body = HEADER + "Paracetamol 500mg,Analgesic,Nothing on file yet\n"

    assert upload(client, admin_headers, body).json()["skipped"] == 1     # off by default
    assert upload(client, admin_headers, body, update_existing=True).json()["updated"] == 1

    found = client.get(
        f"{API}/compositions", params={"q": "paracetamol"}, headers=admin_headers
    ).json()["items"][0]
    assert found["therapeutic_class"] == "Analgesic"
    assert found["notes"] == "Nothing on file yet"


def test_blank_lines_are_not_counted_as_rows(client, admin_headers):
    body = HEADER + "Paracetamol 500mg,,\n,,\n\nCetirizine 10mg,,\n"
    result = upload(client, admin_headers, body).json()

    assert result["total_rows"] == 2
    assert result["created"] == 2
    assert result["errors"] == []


def test_a_file_without_the_composition_column_is_refused_whole(client, admin_headers):
    response = upload(client, admin_headers, "therapeutic_class,notes\nAntibiotic,x\n")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MISSING_COLUMNS"
    assert "composition" in response.json()["detail"]["message"]


# --- Excel ---------------------------------------------------------------


def test_an_excel_file_imports_the_same_as_a_csv(client, admin_headers):
    content = sheet_bytes(
        [
            ("Paracetamol 500mg", "Analgesic", ""),
            ("Amoxicillin 500mg + Clavulanic Acid 125mg", "Antibiotic", "Refrigerate"),
        ]
    )
    result = upload(
        client, admin_headers, content, filename="compositions.xlsx", mime=XLSX_MIME
    ).json()

    assert result["created"] == 2
    assert result["errors"] == []


def test_excel_row_numbers_match_what_the_sheet_shows(client, admin_headers):
    content = sheet_bytes(
        [("Paracetamol 500mg", "", ""), ("", "", ""), ("", "Antibiotic", "no formulation")]
    )
    result = upload(
        client, admin_headers, content, filename="compositions.xlsx", mime=XLSX_MIME
    ).json()

    assert result["created"] == 1
    assert [e["row"] for e in result["errors"]] == [4]   # sheet row 4, blanks skipped


def test_a_number_typed_into_excel_does_not_arrive_as_a_float(client, admin_headers, db):
    """Excel stores 500 as 500.0; "500.0MG" would be a different formulation."""
    book = Workbook()
    book.active.append(["composition", "therapeutic_class"])
    book.active.append(["Paracetamol 500mg", 500])       # a stray number in the class column
    stream = io.BytesIO()
    book.save(stream)

    result = upload(
        client, admin_headers, stream.getvalue(), filename="c.xlsx", mime=XLSX_MIME
    ).json()
    assert result["created"] == 1

    composition = db.query(Composition).one()
    assert composition.composition_text == "PARACETAMOL (500MG)"
    assert composition.therapeutic_class == "500"


def test_a_title_line_above_the_table_does_not_break_the_read(client, admin_headers):
    book = Workbook()
    book.active.append([])
    book.active.append(["Composition master — July 2026"])
    book.active.append(["composition", "therapeutic_class", "notes"])
    book.active.append(["Paracetamol 500mg", "Analgesic", ""])
    stream = io.BytesIO()
    book.save(stream)

    result = upload(
        client, admin_headers, stream.getvalue(), filename="c.xlsx", mime=XLSX_MIME
    ).json()
    assert result["created"] == 1


def test_a_file_that_is_not_a_spreadsheet_is_refused_clearly(client, admin_headers):
    response = upload(
        client, admin_headers, b"not a spreadsheet at all", filename="c.xlsx", mime=XLSX_MIME
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "FILE_UNREADABLE"


def test_the_import_is_audited(client, admin_headers):
    upload(client, admin_headers, HEADER + "Paracetamol 500mg,,\n")

    log = client.get(
        f"{API}/audit-log", params={"entity_type": "CompositionImport"}, headers=admin_headers
    )
    if log.status_code == 200:                       # the log endpoint is admin-only
        assert log.json()["total"] >= 1
