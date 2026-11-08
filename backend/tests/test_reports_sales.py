"""The day-book and the per-executive report."""

from app.utils.clock import business_date
from tests.conftest import API, sell, stocked_medicine

TODAY = business_date()


def fetch(client, headers, path, **params):
    query = {"date_from": TODAY.isoformat(), "date_to": TODAY.isoformat(), **params}
    response = client.get(f"{API}/reports{path}", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def a_sale(client, headers, *, brand, mrp="112.00", qty="10", **kw):
    medicine, _ = stocked_medicine(client, headers, mrp=mrp, brand_name=brand)
    return sell(
        client, headers,
        [{"medicine_id": medicine["id"], "qty_units": qty, "discount_pct": "0"}],
        **kw,
    )


# --- day-book ------------------------------------------------------------


def test_daybook_lists_each_invoice_with_its_tender(client, admin_headers):
    sale = a_sale(client, admin_headers, brand="Daybookol")

    body = fetch(client, admin_headers, "/sales/daybook")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["invoice_no"] == sale["invoice_no"]
    assert row["invoice_date"] == TODAY.isoformat()
    assert row["customer"] == "Walk-in"
    assert row["sales_user"] == "Test Admin"
    assert row["line_count"] == 1
    assert row["net_amount"] == "112.00"
    assert row["payment_modes"] == "CASH 112.00"
    assert row["status"] == "COMPLETED"
    # An instant carries its offset, or the browser reads it as local time.
    assert row["invoice_time"].endswith("+00:00")


def test_daybook_totals_and_reconciles_the_drawer_by_mode(client, admin_headers):
    a_sale(client, admin_headers, brand="Daybookol A")
    a_sale(client, admin_headers, brand="Daybookol B")

    body = fetch(client, admin_headers, "/sales/daybook")

    assert body["totals"]["net_amount"] == "224.00"
    assert body["totals"]["line_count"] == 2
    assert body["meta"]["invoice_count"] == 2
    assert body["meta"]["payment_modes"] == [
        {"mode": "CASH", "payment_count": 2, "amount": "224.00"}
    ]


def test_a_split_payment_is_shown_mode_by_mode(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, mrp="112.00", brand_name="Splitol")
    response = client.post(
        f"{API}/sales",
        json={
            "items": [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "0"}],
            "payments": [
                {"mode": "CASH", "amount": "12.00"},
                {"mode": "UPI", "amount": "100.00"},
            ],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text

    body = fetch(client, admin_headers, "/sales/daybook")

    assert body["rows"][0]["payment_modes"] == "CASH 12.00, UPI 100.00"
    assert body["meta"]["payment_modes"] == [
        {"mode": "CASH", "payment_count": 1, "amount": "12.00"},
        {"mode": "UPI", "payment_count": 1, "amount": "100.00"},
    ]


def test_a_voided_invoice_is_listed_but_carries_no_money(client, admin_headers):
    sale = a_sale(client, admin_headers, brand="Voidbookol")
    request = client.post(
        f"{API}/sales/{sale['id']}/void-request",
        json={"reason": "Rung up twice"},
        headers=admin_headers,
    )
    client.post(
        f"{API}/void-requests/{request.json()['id']}/approve",
        json={"note": "Confirmed"},
        headers=admin_headers,
    )

    body = fetch(client, admin_headers, "/sales/daybook")

    row = body["rows"][0]
    assert row["status"] == "VOIDED"
    assert row["net_amount"] == "0.00"
    assert row["payment_modes"] == ""
    assert body["totals"]["net_amount"] == "0.00"
    assert body["meta"]["voided_count"] == 1
    assert body["meta"]["invoice_count"] == 0


def test_a_cash_refund_is_reported_against_the_drawer(client, admin_headers):
    sale = a_sale(
        client, admin_headers, brand="Refundol",
        customer={"mobile": "9876500021", "name": "Refund Tester"},
    )
    refund = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )
    assert refund.status_code == 201, refund.text

    body = fetch(client, admin_headers, "/sales/daybook")
    assert body["meta"]["cash_refunds"] == "56.00"


# --- by executive --------------------------------------------------------


def test_by_executive_reports_volume_and_average_invoice(client, admin_headers):
    a_sale(client, admin_headers, brand="Execol A")
    a_sale(client, admin_headers, brand="Execol B", qty="5")

    body = fetch(client, admin_headers, "/sales/by-executive")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["sales_user"] == "Test Admin"
    assert row["invoice_count"] == 2
    assert row["units_sold"] == "15.000"
    assert row["net_amount"] == "168.00"          # 112 + 56
    assert row["average_invoice"] == "84.00"
    assert row["voided_count"] == 0
    assert row["void_pct"] == "0.00"


def test_discount_percentage_is_of_gross_not_a_sum_of_percentages(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, mrp="100.00", brand_name="Discountol")
    sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "10"}],
    )

    row = fetch(client, admin_headers, "/sales/by-executive")["rows"][0]

    assert row["gross_amount"] == "100.00"
    assert row["discount_amount"] == "10.00"
    assert row["discount_pct"] == "10.00"


def test_void_and_return_rates_are_attributed_to_the_biller(client, admin_headers):
    kept = a_sale(
        client, admin_headers, brand="Ratiol A",
        customer={"mobile": "9876500031", "name": "Rate Tester"},
    )
    voided = a_sale(client, admin_headers, brand="Ratiol B")

    request = client.post(
        f"{API}/sales/{voided['id']}/void-request",
        json={"reason": "Wrong customer"},
        headers=admin_headers,
    )
    client.post(
        f"{API}/void-requests/{request.json()['id']}/approve",
        json={"note": "Confirmed"},
        headers=admin_headers,
    )
    client.post(
        f"{API}/returns",
        json={
            "sale_id": kept["id"],
            "items": [{"sale_item_id": kept["items"][0]["id"], "qty_units": "5"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )

    row = fetch(client, admin_headers, "/sales/by-executive")["rows"][0]

    assert row["invoice_count"] == 1
    assert row["voided_count"] == 1
    assert row["void_pct"] == "50.00"             # one of the two rung up
    assert row["returned_amount"] == "56.00"
    assert row["return_pct"] == "50.00"           # of ₹112 still billed


def test_each_executive_gets_their_own_row(client, admin_headers, sales_headers):
    a_sale(client, admin_headers, brand="Twoexecol A")
    medicine, _ = stocked_medicine(
        client, admin_headers, mrp="112.00", brand_name="Twoexecol B"
    )
    sell(
        client, sales_headers,
        [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "0"}],
    )

    body = fetch(client, admin_headers, "/sales/by-executive")
    names = {row["sales_user"] for row in body["rows"]}

    assert names == {"Test Admin", "Test Sales"}
    assert body["totals"]["net_amount"] == "224.00"
    assert body["totals"]["invoice_count"] == 2


def test_a_sales_executive_cannot_read_the_daybook(client, sales_headers):
    response = client.get(f"{API}/reports/sales/daybook", headers=sales_headers)
    assert response.status_code == 403
