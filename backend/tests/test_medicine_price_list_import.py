"""Loading a manufacturer's or portal's price list.

The file has its own column names — Medicine Name, Price (INR),
Salt/Composition, Pack Info, Rx Required — and one free-text column carrying
the whole pack description. What is tested here is the translation into the
shop's own structure, and above all the countability rule: a bottle of syrup
must never come out sellable a millilitre at a time.
"""

import io
from decimal import Decimal

from openpyxl import Workbook, load_workbook

from app.models.catalog import Medicine

from tests.conftest import API

HEADER = "Medicine Name,Price (INR),Manufacturer,Salt/Composition,Pack Info,Rx Required\n"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def upload(client, headers, body, *, filename="price-list.csv", mime="text/csv", **params):
    payload = body.encode("utf-8") if isinstance(body, str) else body
    return client.post(
        f"{API}/medicines/import",
        files={"file": (filename, io.BytesIO(payload), mime)},
        headers=headers,
        params=params,
    )


def one(client, headers, brand):
    listed = client.get(f"{API}/medicines", params={"q": brand}, headers=headers).json()
    return next(m for m in listed["items"] if m["brand_name"].lower() == brand.lower())


# --- the file as it arrives ----------------------------------------------


def test_the_price_list_columns_are_recognised_without_being_told(client, admin_headers):
    body = HEADER + "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    result = upload(client, admin_headers, body).json()

    assert result["layout"] == "price_list"
    assert result["created"] == 1
    assert result["errors"] == []


def test_each_column_lands_where_it_belongs(client, admin_headers):
    body = HEADER + "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    upload(client, admin_headers, body)

    medicine = one(client, admin_headers, "Dolo 650")
    assert medicine["manufacturer_name"] == "Micro Labs"
    assert medicine["composition_text"] == "PARACETAMOL (650MG)"
    assert medicine["dosage_form"] == "TABLET"
    assert medicine["pack_size"] == "15.000"
    assert medicine["pack_unit"] == "TABLET"
    assert medicine["units_per_pack"] == 15
    assert medicine["list_mrp_per_pack"] == "35.50"
    assert medicine["is_prescription_required"] is False


def test_the_pack_phrases_from_the_file_translate(client, admin_headers):
    body = HEADER + (
        "Augmentin 625,210.00,GSK,Amoxicillin 500mg + Clavulanic Acid 125mg,"
        "strip of 10 tablets,Yes\n"
        "Monocef 1gm,95.00,Aristo,Ceftriaxone 1000mg,vial of 1 Powder for Injection,Yes\n"
        "Meftal P,88.00,Blue Cross,Paracetamol 250mg,bottle of 60 ml Syrup,No\n"
        "Zerodol Drops,72.00,Ipca,Paracetamol 100mg,bottle of 15 ml Oral Drops,No\n"
        "Cremaffin,180.00,Abbott,Liquid Paraffin 1.25ml,bottle of 30 ml Oral Suspension,No\n"
        "Neurobion Forte,45.00,Merck,Vitamin B Complex 1mg,strip of 1 Tablet DT,No\n"
        "Tetanus Toxoid,25.00,Serum Institute,Tetanus Toxoid 5IU,vial of 1 Injection,Yes\n"
    )
    result = upload(client, admin_headers, body).json()
    assert result["created"] == 7, result

    expected = {
        "Augmentin 625": ("TABLET", "TABLET", 10),
        "Monocef 1gm": ("INJECTION", "VIAL", 1),
        "Meftal P": ("SYRUP", "ML", 1),
        "Zerodol Drops": ("DROPS", "ML", 1),
        "Cremaffin": ("SYRUP", "ML", 1),
        "Neurobion Forte": ("TABLET", "TABLET", 1),
        "Tetanus Toxoid": ("INJECTION", "VIAL", 1),
    }
    for brand, (form, unit, units) in expected.items():
        medicine = one(client, admin_headers, brand)
        assert (medicine["dosage_form"], medicine["pack_unit"], medicine["units_per_pack"]) == (
            form, unit, units,
        ), brand


def test_a_syrup_never_becomes_sellable_by_the_millilitre(client, admin_headers):
    """The rule the whole pack translation exists to protect."""
    body = HEADER + "Syrupy,80.00,Labs,Paracetamol 125mg,bottle of 100 ml Syrup,No\n"
    upload(client, admin_headers, body)

    medicine = one(client, admin_headers, "Syrupy")
    assert medicine["units_per_pack"] == 1
    assert medicine["is_countable"] is False
    assert medicine["pack_size"] == "100.000"     # the volume is still recorded


def test_rx_required_marks_the_medicine_prescription_only(client, admin_headers):
    body = HEADER + (
        "Augmentin 625,210.00,GSK,Amoxicillin 500mg,strip of 10 tablets,Yes\n"
        "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    )
    result = upload(client, admin_headers, body).json()

    rx = one(client, admin_headers, "Augmentin 625")
    otc = one(client, admin_headers, "Dolo 650")
    assert rx["is_prescription_required"] is True
    assert rx["schedule_type"] == "SCHEDULE_H"
    assert otc["is_prescription_required"] is False
    assert otc["schedule_type"] == "OTC"

    # The assumption is stated, not buried: H1 and X carry a register
    # obligation this file cannot express.
    assert any("Schedule H" in w for w in result["warnings"]), result["warnings"]


def test_yes_and_no_are_read_however_they_are_written(client, admin_headers):
    body = HEADER + (
        "One,10,Labs,Salt A 1mg,strip of 10 tablets,TRUE\n"
        "Two,10,Labs,Salt B 1mg,strip of 10 tablets,y\n"
        "Three,10,Labs,Salt C 1mg,strip of 10 tablets,1\n"
        "Four,10,Labs,Salt D 1mg,strip of 10 tablets,OTC\n"
        "Five,10,Labs,Salt E 1mg,strip of 10 tablets,\n"
    )
    assert upload(client, admin_headers, body).json()["created"] == 5

    assert one(client, admin_headers, "One")["is_prescription_required"] is True
    assert one(client, admin_headers, "Two")["is_prescription_required"] is True
    assert one(client, admin_headers, "Three")["is_prescription_required"] is True
    assert one(client, admin_headers, "Four")["is_prescription_required"] is False
    assert one(client, admin_headers, "Five")["is_prescription_required"] is False


def test_an_unreadable_rx_value_is_rejected_by_the_row_not_guessed(client, admin_headers):
    body = HEADER + (
        "Good,10,Labs,Salt A 1mg,strip of 10 tablets,No\n"
        "Odd,10,Labs,Salt B 1mg,strip of 10 tablets,maybe\n"
    )
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 1
    assert result["errors"][0]["row"] == 3
    assert result["errors"][0]["field"] == "rx_required"


# --- the price ------------------------------------------------------------


def test_the_price_is_kept_as_a_reference_not_as_a_sale_price(client, admin_headers, db):
    """A list price must not become a price anybody is charged.

    Billing MRP comes from the batch, because that is the figure printed on
    the box the customer is handed.
    """
    body = HEADER + "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    upload(client, admin_headers, body)

    medicine = one(client, admin_headers, "Dolo 650")
    assert medicine["list_mrp_per_pack"] == "35.50"
    # No stock, so nothing sellable — and therefore no price to charge.
    assert medicine["mrp_per_pack"] is None
    assert medicine["unit_mrp"] is None

    stored = db.get(Medicine, medicine["id"])
    assert stored.list_mrp_per_pack == Decimal("35.50")


def test_a_missing_price_is_simply_absent(client, admin_headers):
    body = HEADER + "Nameless,,Labs,Salt A 1mg,strip of 10 tablets,No\n"
    assert upload(client, admin_headers, body).json()["created"] == 1
    assert one(client, admin_headers, "Nameless")["list_mrp_per_pack"] is None


def test_a_price_that_is_not_a_number_is_rejected(client, admin_headers):
    body = HEADER + "Odd,ask us,Labs,Salt A 1mg,strip of 10 tablets,No\n"
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 0
    assert result["errors"][0]["field"] == "list_mrp_per_pack"


# --- pack info ------------------------------------------------------------


def test_an_unreadable_pack_costs_only_that_row(client, admin_headers):
    body = HEADER + (
        "Good,10,Labs,Salt A 1mg,strip of 10 tablets,No\n"
        "Bad,10,Labs,Salt B 1mg,as per pack,No\n"
        "Also Good,10,Labs,Salt C 1mg,bottle of 30 ml Syrup,No\n"
    )
    result = upload(client, admin_headers, body).json()

    assert result["created"] == 2
    assert [e["row"] for e in result["errors"]] == [3]
    assert result["errors"][0]["field"] == "pack_info"
    assert sorted(m["brand_name"] for m in
                  client.get(f"{API}/medicines", params={"q": "Good"},
                             headers=admin_headers).json()["items"]) == ["Also Good", "Good"]


def test_a_blank_pack_column_falls_back_to_a_single_tablet(client, admin_headers):
    body = HEADER + "Plain,10,Labs,Salt A 1mg,,No\n"
    assert upload(client, admin_headers, body).json()["created"] == 1

    medicine = one(client, admin_headers, "Plain")
    assert medicine["dosage_form"] == "TABLET"
    assert medicine["units_per_pack"] == 1


def test_the_label_keeps_the_container_the_price_list_named(client, admin_headers):
    body = HEADER + (
        "Tubey,10,Labs,Salt A 1mg,tube of 30 gm Cream,No\n"
        "Vially,10,Labs,Salt B 1mg,vial of 1 Injection,No\n"
    )
    upload(client, admin_headers, body)

    assert one(client, admin_headers, "Tubey")["pack_label"] == "30 g tube"
    assert one(client, admin_headers, "Vially")["pack_label"] == "1 vial"


# --- re-uploading ---------------------------------------------------------


def test_the_same_price_list_twice_creates_nothing_the_second_time(client, admin_headers):
    body = HEADER + "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    assert upload(client, admin_headers, body).json()["created"] == 1

    again = upload(client, admin_headers, body).json()
    assert again["created"] == 0
    assert again["skipped"] == 1


def test_a_price_revision_updates_when_asked(client, admin_headers):
    first = HEADER + "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    upload(client, admin_headers, first)

    revised = HEADER + "Dolo 650,39.00,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    assert upload(client, admin_headers, revised, update_existing=True).json()["updated"] == 1
    assert one(client, admin_headers, "Dolo 650")["list_mrp_per_pack"] == "39.00"


def test_an_update_does_not_wipe_what_the_price_list_never_carried(client, admin_headers):
    """A price list has no rack or reorder level; re-uploading must not
    erase the ones the shop set by hand."""
    body = HEADER + "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    upload(client, admin_headers, body)

    medicine = one(client, admin_headers, "Dolo 650")
    client.patch(
        f"{API}/medicines/{medicine['id']}",
        json={"rack_location": "B4", "reorder_level_units": "120"},
        headers=admin_headers,
    )

    upload(client, admin_headers, body, update_existing=True)
    after = one(client, admin_headers, "Dolo 650")
    assert after["rack_location"] == "B4"
    assert after["reorder_level_units"] == "120.000"


# --- choosing the layout --------------------------------------------------


def test_the_shops_own_template_still_reads_as_before(client, admin_headers):
    body = (
        "brand_name,manufacturer,composition,dosage_form,pack_size,pack_unit\n"
        "Pan 40,Alkem,Pantoprazole 40mg,TABLET,15,TABLET\n"
    )
    result = upload(client, admin_headers, body).json()
    assert result["layout"] == "standard"
    assert result["created"] == 1


def test_the_layout_can_be_named_rather_than_sniffed(client, admin_headers):
    body = HEADER + "Dolo 650,35.50,Micro Labs,Paracetamol 650mg,strip of 15 tablets,No\n"
    result = upload(client, admin_headers, body, layout="price_list").json()
    assert result["created"] == 1


def test_a_file_missing_a_required_column_names_it_in_that_layouts_words(
    client, admin_headers
):
    body = "Medicine Name,Price (INR),Pack Info\nDolo 650,35.50,strip of 15 tablets\n"
    response = upload(client, admin_headers, body)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "MISSING_COLUMNS"
    assert "manufacturer" in detail["message"].lower()


# --- Excel ---------------------------------------------------------------


def test_a_price_list_in_excel_reads_the_same(client, admin_headers):
    book = Workbook()
    book.active.append(
        ["Medicine Name", "Price (INR)", "Manufacturer", "Salt/Composition",
         "Pack Info", "Rx Required"]
    )
    book.active.append(
        ["Dolo 650", 35.5, "Micro Labs", "Paracetamol 650mg", "strip of 15 tablets", "No"]
    )
    book.active.append(
        ["Monocef 1gm", 95, "Aristo", "Ceftriaxone 1000mg",
         "vial of 1 Powder for Injection", "Yes"]
    )
    stream = io.BytesIO()
    book.save(stream)

    result = upload(
        client, admin_headers, stream.getvalue(), filename="price-list.xlsx", mime=XLSX_MIME
    ).json()
    assert result["created"] == 2, result
    assert one(client, admin_headers, "Dolo 650")["list_mrp_per_pack"] == "35.50"
    assert one(client, admin_headers, "Monocef 1gm")["pack_unit"] == "VIAL"


def test_a_pack_size_typed_as_a_number_in_excel_is_not_read_as_a_float(client, admin_headers):
    """Excel hands back 15.0; a pack of "15.0 tablets" would be refused."""
    book = Workbook()
    book.active.append(["brand_name", "manufacturer", "composition", "pack_size"])
    book.active.append(["Pan 40", "Alkem", "Pantoprazole 40mg", 15])
    stream = io.BytesIO()
    book.save(stream)

    result = upload(
        client, admin_headers, stream.getvalue(), filename="m.xlsx", mime=XLSX_MIME
    ).json()
    assert result["created"] == 1, result
    assert one(client, admin_headers, "Pan 40")["units_per_pack"] == 15


# --- templates ------------------------------------------------------------


def test_both_medicine_templates_download(client, admin_headers):
    for layout in ("standard", "price_list"):
        csv_file = client.get(
            f"{API}/medicines/import-template",
            params={"format": "csv", "layout": layout}, headers=admin_headers,
        )
        assert csv_file.status_code == 200
        assert "attachment" in csv_file.headers["content-disposition"]

        excel = client.get(
            f"{API}/medicines/import-template",
            params={"format": "xlsx", "layout": layout}, headers=admin_headers,
        )
        assert excel.status_code == 200
        assert "spreadsheetml" in excel.headers["content-type"]


def test_the_price_list_template_carries_the_files_own_headers(client, admin_headers):
    excel = client.get(
        f"{API}/medicines/import-template",
        params={"format": "xlsx", "layout": "price_list"}, headers=admin_headers,
    )
    book = load_workbook(io.BytesIO(excel.content))
    assert [c.value for c in book.worksheets[0][1]] == [
        "Medicine Name", "Price (INR)", "Manufacturer", "Salt/Composition",
        "Pack Info", "Rx Required",
    ]
    notes = book["How to fill this in"]
    written = "\n".join(str(c.value) for row in notes.iter_rows() for c in row if c.value)
    assert "strip of 10 tablets" in written
    assert "Schedule H" in written


def test_a_downloaded_template_is_a_file_the_importer_accepts(client, admin_headers):
    """The header handed out has to be the header taken back."""
    for layout in ("standard", "price_list"):
        template = client.get(
            f"{API}/medicines/import-template",
            params={"format": "csv", "layout": layout}, headers=admin_headers,
        )
        header = template.text.lstrip("﻿").strip()
        result = upload(client, admin_headers, header + "\n")
        assert result.status_code == 200, (layout, result.text)
        assert result.json()["total_rows"] == 0


def test_the_templates_are_admin_only(client, sales_headers):
    assert (
        client.get(f"{API}/medicines/import-template", headers=sales_headers).status_code == 403
    )
