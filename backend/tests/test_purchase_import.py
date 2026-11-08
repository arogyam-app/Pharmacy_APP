"""Receiving a supplier's item list from a spreadsheet.

The file carries items only — the supplier and the invoice number are keyed
on screen, because they identify the document. And the file is never
*applied*: it comes back as rows with candidates, and a person resolves each
one before any stock moves.
"""

import csv
import io
from datetime import timedelta
from decimal import Decimal

from app.utils.clock import business_date
from tests.conftest import API, make_medicine

TODAY = business_date()

HEADERS = [
    "product", "batch_no", "expiry_date", "mfg_date", "best_before_months",
    "qty_packs", "free_qty_packs", "mrp_per_pack", "rate_per_pack",
    "discount_pct", "gst_rate", "default_discount_pct", "max_discount_pct",
]


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def sheet(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in HEADERS})
    return buffer.getvalue().encode("utf-8")


def line(**overrides) -> dict:
    row = {
        "product": "Paracetamol 500 TAB",
        "batch_no": "B1",
        "expiry_date": iso(400),
        "qty_packs": "10",
        "mrp_per_pack": "150",
        "rate_per_pack": "100",
        "gst_rate": "12",
    }
    row.update(overrides)
    return row


def preview(client, headers, rows, filename="items.csv"):
    response = client.post(
        f"{API}/purchases/import-preview",
        files={"file": (filename, sheet(rows), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- the template ---------------------------------------------------------


def test_a_csv_template_can_be_downloaded(client, admin_headers):
    response = client.get(f"{API}/purchases/import-template?format=csv", headers=admin_headers)
    assert response.status_code == 200
    text = response.text
    for column in ("product", "batch_no", "qty_packs", "mrp_per_pack", "rate_per_pack"):
        assert column in text
    # Items only — the supplier identifies the document and is keyed on screen.
    assert "supplier" not in text.lower()


def test_an_excel_template_can_be_downloaded(client, admin_headers):
    response = client.get(f"{API}/purchases/import-template?format=xlsx", headers=admin_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats"
    )
    assert response.content[:2] == b"PK"  # a zip, which is what xlsx is


# --- reading the file -----------------------------------------------------


def test_the_figures_are_parsed_off_each_row(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = preview(client, admin_headers, [line(qty_packs="12", free_qty_packs="2")])
    row = body["lines"][0]

    assert body["total_rows"] == 1
    assert Decimal(row["qty_packs"]) == Decimal("12")
    assert Decimal(row["free_qty_packs"]) == Decimal("2")
    assert Decimal(row["mrp_per_pack"]) == Decimal("150")
    assert Decimal(row["cost_per_pack"]) == Decimal("100")
    assert row["expiry_basis"] == "EXPIRY_DATE"


def test_a_best_before_row_is_read_as_one(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = preview(
        client, admin_headers,
        [line(expiry_date="", mfg_date=iso(-30), best_before_months="24")],
    )
    row = body["lines"][0]

    assert row["expiry_basis"] == "BEST_BEFORE"
    assert row["best_before_months"] == 24
    assert not row["errors"]


def test_a_row_with_no_shelf_life_is_read_as_never_expiring(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = preview(client, admin_headers, [line(expiry_date="")])
    assert body["lines"][0]["expiry_basis"] == "NONE"


def test_an_expiry_given_as_month_year_becomes_the_last_day_of_that_month(
    client, admin_headers
):
    """A pack is printed "EXP 08/26" far more often than with a day."""
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = preview(client, admin_headers, [line(expiry_date="08/26")])
    row = body["lines"][0]

    assert not row["errors"]
    assert row["expiry_basis"] == "EXPIRY_DATE"
    assert row["expiry_date"] == "2026-08-31"


def test_a_four_digit_year_also_works(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = preview(client, admin_headers, [line(expiry_date="08/2026")])
    assert body["lines"][0]["expiry_date"] == "2026-08-31"


def test_a_month_year_expiry_respects_leap_years(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = preview(
        client, admin_headers,
        [line(expiry_date="02/24", batch_no="B1"), line(expiry_date="02/26", batch_no="B2")],
    )
    rows = body["lines"]

    assert rows[0]["expiry_date"] == "2024-02-29"  # 2024 is a leap year
    assert rows[1]["expiry_date"] == "2026-02-28"  # 2026 is not


def test_an_out_of_range_month_year_is_still_an_error(client, admin_headers):
    body = preview(client, admin_headers, [line(expiry_date="13/26")])
    assert any("expiry_date" in e for e in body["lines"][0]["errors"])


def test_a_manufacturing_date_does_not_accept_month_year(client, admin_headers):
    """Reading a manufacturing month as its *last* day would push a
    best-before shelf life later than the pack can support — the wrong
    direction to guess wrong in, so mfg_date stays strict."""
    body = preview(
        client, admin_headers,
        [line(expiry_date="", mfg_date="06/24", best_before_months="24")],
    )
    assert any("mfg_date" in e for e in body["lines"][0]["errors"])


def test_half_a_best_before_is_an_error(client, admin_headers):
    body = preview(client, admin_headers, [line(expiry_date="", mfg_date=iso(-30))])
    assert any("best_before_months" in e for e in body["lines"][0]["errors"])


def test_every_fault_in_a_row_is_reported_at_once(client, admin_headers):
    """Somebody fixing a file wants all of a row's problems, not the first."""
    body = preview(
        client, admin_headers,
        [line(product="", batch_no="", qty_packs="nope", mrp_per_pack="0")],
    )
    errors = " ".join(body["lines"][0]["errors"])

    assert "product" in errors
    assert "batch_no" in errors
    assert "qty_packs" in errors


def test_blank_rows_are_skipped(client, admin_headers):
    body = preview(client, admin_headers, [line(), {}, line(batch_no="B2")])
    assert body["total_rows"] == 2


def test_a_file_missing_a_column_says_which(client, admin_headers):
    raw = b"product,batch_no,qty_packs\nParacetamol TAB,B1,10\n"
    response = client.post(
        f"{API}/purchases/import-preview",
        files={"file": ("items.csv", raw, "text/csv")},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert "mrp_per_pack" in response.json()["missing_columns"]


# --- matching the supplier's wording --------------------------------------


def test_a_trailing_form_word_still_finds_the_product(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500", pack_size="10")

    body = preview(client, admin_headers, [line(product="PARACETAMOL 500 TABS")])
    row = body["lines"][0]

    assert [m["medicine"]["brand_name"] for m in row["matches"]] == ["Paracetamol 500"]


def test_nothing_is_resolved_for_the_operator(client, admin_headers):
    """The contract: candidates, never a choice. No medicine_id comes back."""
    make_medicine(client, admin_headers, brand_name="Paracetamol 500")

    body = preview(client, admin_headers, [line()])
    row = body["lines"][0]

    assert "medicine_id" not in row
    assert row["matches"]


def test_a_row_matching_nothing_says_so(client, admin_headers):
    body = preview(client, admin_headers, [line(product="ZZZQQQ TAB")])
    row = body["lines"][0]

    assert row["matches"] == []
    assert row["has_pack_match"] is False


def test_two_sizes_of_a_brand_are_told_apart(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Volini Gel",
                  dosage_form="OINTMENT", pack_size="10", pack_unit="GM")
    make_medicine(client, admin_headers, brand_name="Volini Gel",
                  dosage_form="OINTMENT", pack_size="30", pack_unit="GM")

    body = preview(client, admin_headers, [line(product="VOLINI GEL 30GM")])
    row = body["lines"][0]

    assert row["pack_hint"]["text"] == "30GM"
    assert row["has_pack_match"] is True
    assert row["matches"][0]["pack_matches"] is True
    assert any(m["pack_matches"] is False for m in row["matches"])


def test_a_size_not_on_the_master_is_flagged_for_adding(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Volini Gel",
                  dosage_form="OINTMENT", pack_size="30", pack_unit="GM")

    body = preview(client, admin_headers, [line(product="VOLINI GEL 20GM")])
    row = body["lines"][0]

    assert row["pack_hint"]["text"] == "20GM"
    assert row["has_pack_match"] is False
    # The 30GM row is offered, marked as the different product it is.
    assert row["matches"] and all(not m["pack_matches"] for m in row["matches"])


def test_ready_counts_rows_with_nothing_left_to_decide(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Paracetamol 500", pack_size="10")

    body = preview(
        client, admin_headers,
        [line(product="PARACETAMOL 500 TAB"), line(product="ZZZQQQ", batch_no="B2")],
    )
    assert body["total_rows"] == 2
    assert body["ready_rows"] == 1


# --- it receives nothing --------------------------------------------------


def test_previewing_creates_no_stock_and_no_invoice(client, admin_headers, db):
    from app.models.inventory import Batch
    from app.models.purchase import PurchaseInvoice

    make_medicine(client, admin_headers, brand_name="Paracetamol 500")
    preview(client, admin_headers, [line(), line(batch_no="B2")])

    assert db.query(PurchaseInvoice).count() == 0
    assert db.query(Batch).count() == 0


def test_a_sales_executive_without_receive_goods_cannot_upload(client, sales_headers):
    response = client.post(
        f"{API}/purchases/import-preview",
        files={"file": ("items.csv", sheet([line()]), "text/csv")},
        headers=sales_headers,
    )
    assert response.status_code == 403
