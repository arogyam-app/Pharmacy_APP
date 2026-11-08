"""Profitability, the ranked lists, the credit-note ledger and the audit report."""

from decimal import Decimal

from app.utils.clock import business_date
from tests.conftest import API, make_medicine, make_purchase, make_supplier, sell

TODAY = business_date()


def fetch(client, headers, path, **params):
    query = {"date_from": TODAY.isoformat(), "date_to": TODAY.isoformat(), **params}
    response = client.get(f"{API}/reports{path}", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def bought_and_sold(client, headers, *, brand, qty_units="10", invoice_no="SUP-001", **purchase):
    """A medicine received at a known cost, then sold. Cost ₹10, MRP ₹15 a unit."""
    supplier = make_supplier(client, headers)
    medicine = make_medicine(client, headers, brand_name=brand, **purchase)
    make_purchase(
        client, headers, supplier["id"], medicine["id"], supplier_invoice_no=invoice_no
    )
    sale = sell(
        client, headers,
        [{"medicine_id": medicine["id"], "qty_units": qty_units, "discount_pct": "0"}],
        customer={"mobile": "9876500041", "name": "Margin Tester"},
    )
    return medicine, sale


# --- profitability -------------------------------------------------------


def test_profitability_is_revenue_ex_gst_less_snapshotted_cost(client, admin_headers):
    # 10 units at ₹15 MRP = ₹150 billed. MRP includes 12 % GST, so revenue is
    # 150 / 1.12 = 133.93. Cost is 10 units at ₹10 = ₹100.
    bought_and_sold(client, admin_headers, brand="Marginol")

    body = fetch(client, admin_headers, "/profitability")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["group"] == "Marginol"
    assert row["units_sold"] == "10.000"
    assert row["sale_value"] == "150.00"
    assert row["revenue"] == "133.93"
    assert row["cost_value"] == "100.00"
    assert row["margin_value"] == "33.93"
    assert row["margin_pct"] == "25.33"
    assert body["meta"]["lines_without_cost"] == 0


def test_a_return_is_netted_off_both_revenue_and_cost(client, admin_headers):
    _, sale = bought_and_sold(client, admin_headers, brand="Netol")

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

    row = fetch(client, admin_headers, "/profitability")["rows"][0]

    assert row["units_sold"] == "5.000"
    assert row["cost_value"] == "50.00"           # half the cost came back too
    assert Decimal(row["margin_value"]) == Decimal(row["revenue"]) - Decimal("50.00")


def test_profitability_groups_by_manufacturer_and_by_month(client, admin_headers):
    bought_and_sold(client, admin_headers, brand="Groupol", manufacturer_name="Acme Labs")

    by_maker = fetch(client, admin_headers, "/profitability", group_by="manufacturer")
    assert by_maker["rows"][0]["group"] == "Acme Labs"
    assert by_maker["columns"][0]["label"] == "Manufacturer"
    assert by_maker["meta"]["group_by"] == "manufacturer"

    by_month = fetch(client, admin_headers, "/profitability", group_by="month")
    assert by_month["rows"][0]["group"] == TODAY.strftime("%Y-%m")


def test_an_unknown_grouping_is_rejected(client, admin_headers):
    response = client.get(
        f"{API}/reports/profitability", params={"group_by": "colour"}, headers=admin_headers
    )
    assert response.status_code == 422


def test_stock_that_was_never_bought_in_is_flagged(client, admin_headers):
    from tests.conftest import stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Freeol", mrp="112.00")
    sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": "10"}])

    body = fetch(client, admin_headers, "/profitability")

    assert body["rows"][0]["cost_value"] == "0.00"
    assert body["meta"]["lines_without_cost"] == 1


# --- ranked lists --------------------------------------------------------


def test_top_customers_ranks_by_value_billed(client, admin_headers):
    from tests.conftest import stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Loyalol", mrp="100.00")
    for mobile, units in (("9800000001", "10"), ("9800000002", "30")):
        sell(
            client, admin_headers,
            [{"medicine_id": medicine["id"], "qty_units": units, "discount_pct": "0"}],
            customer={"mobile": mobile, "name": f"Buyer {mobile[-1]}"},
        )

    body = fetch(client, admin_headers, "/customers/top")

    assert [row["mobile"] for row in body["rows"]] == ["9800000002", "9800000001"]
    assert body["rows"][0]["net_amount"] == "300.00"
    assert body["rows"][0]["invoice_count"] == 1
    assert body["rows"][0]["average_invoice"] == "300.00"
    assert body["rows"][0]["last_visit"] == TODAY.isoformat()


def test_an_anonymous_walk_in_is_left_out_of_the_customer_ranking(client, admin_headers):
    from tests.conftest import stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Walkinol")
    sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": "10"}])

    assert fetch(client, admin_headers, "/customers/top")["rows"] == []


def test_top_doctors_counts_prescriptions_and_value(client, admin_headers):
    from tests.conftest import stocked_medicine

    doctor = client.post(
        f"{API}/doctors",
        json={"name": "Dr Rao", "registration_no": "AP-1234"},
        headers=admin_headers,
    )
    assert doctor.status_code == 201, doctor.text
    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Rxol", mrp="100.00")
    sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "0"}],
        doctor_id=doctor.json()["id"],
        customer={"mobile": "9800000009", "name": "Patient One"},
    )

    body = fetch(client, admin_headers, "/doctors/top")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["doctor"] == "Dr Rao"
    assert row["registration_no"] == "AP-1234"
    assert row["prescription_count"] == 1
    assert row["patient_count"] == 1
    assert row["net_amount"] == "100.00"


# --- credit notes --------------------------------------------------------


def test_the_credit_note_ledger_shows_issued_used_and_outstanding(client, admin_headers):
    from tests.conftest import stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Creditol", mrp="100.00")
    sale = sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "0"}],
        customer={"mobile": "9800000021", "name": "Credit Tester"},
    )
    issued = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}],
            "refund_mode": "CREDIT_NOTE",
        },
        headers=admin_headers,
    )
    assert issued.status_code == 201, issued.text

    body = fetch(client, admin_headers, "/credit-notes/ledger")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["customer"] == "Credit Tester"
    assert row["mobile"] == "9800000021"
    assert row["amount_issued"] == "50.00"
    assert row["amount_used"] == "0.00"
    assert row["balance_amount"] == "50.00"
    assert row["status"] == "OPEN"
    assert row["days_to_expiry"] == 180
    assert body["meta"]["outstanding"] == "50.00"
    assert body["meta"]["expiring_within_30_days"] == "0.00"


# --- audit ---------------------------------------------------------------


def test_the_audit_report_lists_the_void_and_its_approval(client, admin_headers):
    from tests.conftest import stocked_medicine

    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Auditol")
    sale = sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": "10"}])

    request = client.post(
        f"{API}/sales/{sale['id']}/void-request",
        json={"reason": "Customer changed their mind"},
        headers=admin_headers,
    )
    approve = client.post(
        f"{API}/void-requests/{request.json()['id']}/approve",
        json={"note": "Stock back on the shelf"},
        headers=admin_headers,
    )
    assert approve.status_code == 200, approve.text

    body = fetch(client, admin_headers, "/audit")

    actions = {row["action"] for row in body["rows"]}
    assert "APPROVE" in actions
    assert body["meta"]["entry_count"] == len(body["rows"])
    assert body["rows"][0]["user"] == "Test Admin"
    assert body["rows"][0]["at"].endswith("+00:00")


def test_ordinary_edits_stay_out_of_the_audit_report(client, admin_headers):
    from tests.conftest import stocked_medicine

    stocked_medicine(client, admin_headers, brand_name="Quietol")

    body = fetch(client, admin_headers, "/audit")
    assert body["rows"] == []


# --- access --------------------------------------------------------------


def test_a_sales_executive_cannot_read_profitability(client, sales_headers):
    response = client.get(f"{API}/reports/profitability", headers=sales_headers)
    assert response.status_code == 403
