"""The three GST reports.

These are the compliance-critical ones, so the tests here check arithmetic
against figures worked by hand rather than against whatever the code happens
to produce: an MRP-inclusive sale at 12 %, a purchase at 12 %, and the
netting of a credit note.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.utils.clock import business_date
from tests.conftest import (
    API,
    make_medicine,
    make_purchase,
    make_supplier,
    sell,
    stocked_medicine,
)

TODAY = business_date()


def rows_by(body, key):
    return {row[key]: row for row in body["rows"]}


def fetch(client, headers, path, **params):
    query = {"date_from": TODAY.isoformat(), "date_to": TODAY.isoformat(), **params}
    response = client.get(f"{API}/reports{path}", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def a_sale(client, headers, *, brand, mrp="112.00", qty="1", customer=None, discount="0"):
    medicine, _ = stocked_medicine(client, headers, mrp=mrp, brand_name=brand)
    return sell(
        client, headers,
        [{"medicine_id": medicine["id"], "qty_units": qty, "discount_pct": discount}],
        **({"customer": customer} if customer else {}),
    )


# --- outward -------------------------------------------------------------


def test_outward_summary_splits_an_mrp_inclusive_sale_into_taxable_and_tax(
    client, admin_headers
):
    # One pack of 10 tablets at ₹112, sold whole, 12 % GST included in MRP.
    # Taxable = 112 / 1.12 = 100.00, tax = 12.00, split 6/6 intra-state.
    a_sale(client, admin_headers, brand="Gstolol 10", mrp="112.00", qty="10")

    body = fetch(client, admin_headers, "/gst/outward-summary")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["customer_type"] == "B2C"
    assert row["gst_rate"] == "12.00"
    assert row["invoice_count"] == 1
    assert row["taxable_value"] == "100.00"
    assert row["cgst_amount"] == "6.00"
    assert row["sgst_amount"] == "6.00"
    assert row["igst_amount"] == "0.00"
    assert row["total_tax"] == "12.00"
    assert row["invoice_value"] == "112.00"
    assert row["net_tax"] == "12.00"


def test_outward_summary_separates_rates_and_totals_them(client, admin_headers):
    a_sale(client, admin_headers, brand="Gstolol 12", mrp="112.00", qty="10")
    five = make_medicine(client, admin_headers, brand_name="Gstolol 5", pack_size="10")
    client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": five["id"],
            "batch_no": "B5",
            "expiry_date": (TODAY + timedelta(days=365)).isoformat(),
            "qty_packs": "10",
            "mrp_per_pack": "105.00",
            "gst_rate": "5",
        },
        headers=admin_headers,
    )
    sell(client, admin_headers, [{"medicine_id": five["id"], "qty_units": "10", "discount_pct": "0"}])

    body = fetch(client, admin_headers, "/gst/outward-summary")
    rows = rows_by(body, "gst_rate")

    assert rows["12.00"]["taxable_value"] == "100.00"
    assert rows["5.00"]["taxable_value"] == "100.00"       # 105 / 1.05
    assert rows["5.00"]["total_tax"] == "5.00"
    assert body["totals"]["taxable_value"] == "200.00"
    assert body["totals"]["total_tax"] == "17.00"
    assert body["totals"]["invoice_count"] == 2


def test_a_gstin_on_the_invoice_makes_the_supply_b2b(client, admin_headers):
    a_sale(client, admin_headers, brand="Gstolol B2C", mrp="112.00", qty="10")
    a_sale(
        client, admin_headers, brand="Gstolol B2B", mrp="112.00", qty="10",
        customer={"mobile": "9876512345", "name": "Clinic Ltd", "gstin": "18AABCU9603R1ZM"},
    )

    body = fetch(client, admin_headers, "/gst/outward-summary")
    rows = rows_by(body, "customer_type")

    assert rows["B2B"]["taxable_value"] == "100.00"
    assert rows["B2C"]["taxable_value"] == "100.00"


def test_a_voided_sale_leaves_the_outward_summary(client, admin_headers):
    sale = a_sale(client, admin_headers, brand="Gstolol Void", mrp="112.00", qty="10")

    request = client.post(
        f"{API}/sales/{sale['id']}/void-request",
        json={"reason": "Billed to the wrong customer"},
        headers=admin_headers,
    )
    assert request.status_code == 201, request.text
    approve = client.post(
        f"{API}/void-requests/{request.json()['id']}/approve",
        json={"note": "Confirmed at the counter"},
        headers=admin_headers,
    )
    assert approve.status_code == 200, approve.text

    body = fetch(client, admin_headers, "/gst/outward-summary")
    assert body["rows"] == []


def test_a_return_nets_off_the_outward_tax_as_a_credit_note(client, admin_headers):
    sale = a_sale(
        client, admin_headers, brand="Gstolol Ret", mrp="112.00", qty="10",
        customer={"mobile": "9876500009", "name": "Return Tester"},
    )
    returned = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )
    assert returned.status_code == 201, returned.text

    row = fetch(client, admin_headers, "/gst/outward-summary")["rows"][0]

    # Half the pack came back: ₹56 of a ₹112 sale, so ₹50 taxable and ₹6 tax.
    assert row["taxable_value"] == "100.00"
    assert row["total_tax"] == "12.00"
    assert row["credit_taxable"] == "50.00"
    assert row["credit_tax"] == "6.00"
    assert row["net_taxable"] == "50.00"
    assert row["net_tax"] == "6.00"


def test_a_credit_note_raised_after_the_sale_period_still_reduces_liability(
    client, admin_headers, db
):
    """A return in July against a June invoice belongs in July's return."""
    sale = a_sale(
        client, admin_headers, brand="Gstolol Late", mrp="112.00", qty="10",
        customer={"mobile": "9876500010", "name": "Late Returner"},
    )
    client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "10"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )

    # Backdate the invoice so only the credit note falls in the window.
    from app.models.sale import Sale

    invoice = db.query(Sale).filter(Sale.id == sale["id"]).one()
    invoice.invoice_date = TODAY - timedelta(days=40)
    db.commit()

    row = fetch(client, admin_headers, "/gst/outward-summary")["rows"][0]
    assert row["invoice_count"] == 0
    assert row["taxable_value"] == "0.00"
    assert row["credit_tax"] == "12.00"
    assert row["net_tax"] == "-12.00"


# --- inward --------------------------------------------------------------


def test_inward_summary_reports_the_purchase_register_by_supplier_and_rate(
    client, admin_headers
):
    supplier = make_supplier(client, admin_headers, name="Meditrust", gstin="18AAACM1234M1Z5")
    medicine = make_medicine(client, admin_headers, brand_name="Inwardol")
    # 10 packs at ₹100 = ₹1000 taxable, 12 % = ₹120, split 60/60.
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    body = fetch(client, admin_headers, "/gst/inward-summary")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["supplier_name"] == "Meditrust"
    assert row["supplier_gstin"] == "18AAACM1234M1Z5"
    assert row["gst_rate"] == "12.00"
    assert row["invoice_count"] == 1
    assert row["taxable_value"] == "1000.00"
    assert row["cgst_amount"] == "60.00"
    assert row["sgst_amount"] == "60.00"
    assert row["total_tax"] == "120.00"


def test_a_cancelled_purchase_leaves_the_inward_summary(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers, brand_name="Cancelol")
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    cancelled = client.post(
        f"{API}/purchases/{purchase['id']}/cancel",
        json={"reason": "Consignment refused at the door"},
        headers=admin_headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    assert fetch(client, admin_headers, "/gst/inward-summary")["rows"] == []


# --- net liability -------------------------------------------------------


def test_net_liability_is_outward_tax_less_input_credit(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers, brand_name="Liabilitol")
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])   # ₹120 input
    sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": "1"}])

    body = fetch(client, admin_headers, "/gst/net-liability")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["month"] == TODAY.strftime("%Y-%m")
    assert row["gst_rate"] == "12.00"
    assert row["input_credit"] == "120.00"
    # More tax paid to the supplier than collected at the counter, which is
    # exactly what a month of stocking up looks like.
    assert Decimal(row["net_liability"]) == Decimal(row["outward_tax"]) - Decimal("120.00")
    assert body["totals"]["input_credit"] == "120.00"


# --- shape, access and exports ------------------------------------------


def test_reports_carry_the_store_gstin_and_the_window(client, admin_headers):
    body = fetch(client, admin_headers, "/gst/outward-summary")

    assert body["report"] == "gst_outward_summary"
    assert body["date_from"] == TODAY.isoformat()
    assert body["date_to"] == TODAY.isoformat()
    assert body["meta"]["store_name"] == "Test Pharmacy"
    assert body["meta"]["state_code"] == "18"
    assert [c["key"] for c in body["columns"]][:2] == ["customer_type", "gst_rate"]
    # An instant must carry its offset, or the browser reads it as local time.
    assert body["generated_at"].endswith("+00:00")


def test_the_range_defaults_to_the_current_month(client, admin_headers):
    a_sale(client, admin_headers, brand="Gstolol Month", mrp="112.00", qty="10")

    body = client.get(f"{API}/reports/gst/outward-summary", headers=admin_headers).json()

    assert body["date_from"] == TODAY.replace(day=1).isoformat()
    assert body["date_to"] == TODAY.isoformat()
    assert body["rows"][0]["taxable_value"] == "100.00"


def test_a_backwards_range_is_rejected(client, admin_headers):
    response = client.get(
        f"{API}/reports/gst/outward-summary",
        params={"date_from": TODAY.isoformat(), "date_to": (TODAY - timedelta(days=1)).isoformat()},
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_DATE_RANGE"


@pytest.mark.parametrize(
    "path",
    ["/gst/outward-summary", "/gst/inward-summary", "/gst/net-liability"],
)
def test_a_sales_executive_cannot_read_a_report(client, sales_headers, path):
    response = client.get(f"{API}/reports{path}", headers=sales_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_ROLE"


def test_csv_export_carries_the_header_and_a_totals_row(client, admin_headers):
    a_sale(client, admin_headers, brand="Gstolol Csv", mrp="112.00", qty="10")

    response = client.get(
        f"{API}/reports/gst/outward-summary",
        params={"date_from": TODAY.isoformat(), "date_to": TODAY.isoformat(), "format": "csv"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert ".csv" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    assert "Taxable value" in lines[0]
    assert "B2C" in lines[1]
    assert lines[-1].startswith("Total")


def test_xlsx_export_returns_a_spreadsheet(client, admin_headers):
    a_sale(client, admin_headers, brand="Gstolol Xlsx", mrp="112.00", qty="10")

    response = client.get(
        f"{API}/reports/gst/outward-summary",
        params={"date_from": TODAY.isoformat(), "date_to": TODAY.isoformat(), "format": "xlsx"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert ".xlsx" in response.headers["content-disposition"]
    # A real workbook, and the money column arrives as a number rather than
    # as text — otherwise nobody can sum it.
    import io

    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(response.content)).worksheets[0]
    values = [[cell.value for cell in row] for row in sheet.iter_rows()]
    header = next(row for row in values if row and row[0] == "Supply type")
    taxable_at = header.index("Taxable value")
    data = values[values.index(header) + 1]
    assert data[0] == "B2C"
    assert data[taxable_at] == 100
