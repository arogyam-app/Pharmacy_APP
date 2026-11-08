"""Returns and the credit notes they issue.

The phase's acceptance criterion is here: a return issues a credit note
whose balance is consumed exactly once on the next invoice.
"""

from app.utils.clock import business_date
from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import API, make_batch, make_medicine, sell, stocked_medicine


def line_of(medicine_id, qty="1", **kw):
    return {"medicine_id": medicine_id, "qty_units": qty, **kw}


_brand_seq = iter(range(1, 100))


def a_sale(client, headers, mobile="9876500001", qty="20", **kw):
    """One invoice for a named customer, ready to return against.

    Each call catalogues its own brand, so a test can raise two independent
    invoices without the medicine master rejecting a duplicate.
    """
    kw.setdefault("brand_name", f"Returnol {next(_brand_seq)}")
    medicine, batch = stocked_medicine(client, headers, mrp="100.00", **kw)
    sale = sell(
        client, headers,
        [line_of(medicine["id"], qty, discount_pct="0")],
        customer={"mobile": mobile, "name": "Return Tester"},
    )
    return medicine, batch, sale


# --- what can come back ---------------------------------------------------


def test_returnable_shows_what_is_left_per_line(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)

    body = client.get(
        f"{API}/returns/returnable/{sale['id']}", headers=admin_headers
    ).json()

    assert body["can_return"] is True
    assert body["within_window"] is True
    line = body["items"][0]
    assert line["qty_sold_units"] == "20.000"
    assert line["qty_returned_units"] == "0.000"
    assert line["qty_returnable_units"] == "20.000"
    assert line["refund_if_all"] == "200.00"     # 20 units at ₹10


def test_a_partial_return_reduces_what_is_left(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)

    client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )
    body = client.get(
        f"{API}/returns/returnable/{sale['id']}", headers=admin_headers
    ).json()
    assert body["items"][0]["qty_returned_units"] == "5.000"
    assert body["items"][0]["qty_returnable_units"] == "15.000"


def test_returning_more_than_was_sold_is_refused(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)

    response = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "25"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETURN_QTY_EXCEEDS_SOLD"


def test_two_returns_cannot_exceed_the_quantity_sold(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)
    item_id = sale["items"][0]["id"]

    for qty in ("12", "8"):
        assert client.post(
            f"{API}/returns",
            json={"sale_id": sale["id"], "items": [{"sale_item_id": item_id, "qty_units": qty}],
                  "refund_mode": "CASH"},
            headers=admin_headers,
        ).status_code == 201

    third = client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"], "items": [{"sale_item_id": item_id, "qty_units": "1"}],
              "refund_mode": "CASH"},
        headers=admin_headers,
    )
    assert third.status_code == 409
    assert third.json()["detail"]["code"] == "RETURN_QTY_EXCEEDS_SOLD"


def test_a_line_from_another_invoice_is_refused(client, admin_headers):
    _, _, first = a_sale(client, admin_headers)
    _, _, second = a_sale(client, admin_headers, mobile="9876500002")

    response = client.post(
        f"{API}/returns",
        json={
            "sale_id": first["id"],
            "items": [{"sale_item_id": second["items"][0]["id"], "qty_units": "1"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )
    assert response.status_code == 404


# --- the money ------------------------------------------------------------


def test_the_refund_is_the_line_value_pro_rata(client, admin_headers):
    """20 units at ₹10 with 10 % off; 5 back should refund ₹45, not ₹50."""
    medicine, _ = stocked_medicine(client, admin_headers, mrp="100.00")
    sale = sell(
        client, admin_headers, [line_of(medicine["id"], "20", discount_pct="10")],
        customer={"mobile": "9876500003"},
    )

    body = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    ).json()

    assert body["total_refund_amount"] == "45.00"
    assert body["items"][0]["discount_pct"] == "10.00"


def test_gst_is_reversed_at_the_rate_that_was_charged(client, admin_headers):
    medicine, batch = stocked_medicine(client, admin_headers, mrp="100.00")
    sale = sell(
        client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")],
        customer={"mobile": "9876500004"},
    )

    # The slab changes after the sale.
    client.patch(
        f"{API}/inventory/batches/{batch['id']}", json={"gst_rate": "28"},
        headers=admin_headers,
    )

    body = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "10"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    ).json()

    assert body["items"][0]["gst_rate"] == "12.00"       # the sale's rate
    assert body["taxable_amount"] == "89.29"             # 100 / 1.12
    assert Decimal(body["cgst_amount"]) + Decimal(body["sgst_amount"]) == Decimal("10.71")


def test_the_refund_totals_reconcile_with_the_lines(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)

    body = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "7"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    ).json()

    lines = body["items"]
    assert sum(Decimal(i["refund_amount"]) for i in lines) == Decimal(
        body["total_refund_amount"]
    )
    assert sum(Decimal(i["taxable_value"]) for i in lines) == Decimal(body["taxable_amount"])


# --- what happens to the goods -------------------------------------------


def test_saleable_goods_go_back_into_the_original_batch(client, admin_headers, db):
    from app.models.inventory import Batch

    _, batch, sale = a_sale(client, admin_headers)
    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == Decimal("80.000")

    client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5",
                       "condition": "SALEABLE"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )
    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == Decimal("85.000")


def test_damaged_goods_are_refunded_but_never_restocked(client, admin_headers, db):
    """Putting damaged stock back is how it gets sold to the next customer."""
    from app.models.inventory import Batch

    _, batch, sale = a_sale(client, admin_headers)

    body = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5",
                       "condition": "DAMAGED"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    ).json()

    assert body["total_refund_amount"] == "50.00"       # the customer is paid
    assert body["items"][0]["restocked"] is False
    assert body["restocked"] is False
    assert any("damaged or expired" in w for w in body["warnings"])

    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == Decimal("80.000")   # unchanged


def test_a_return_writes_a_ledger_row_and_keeps_the_invariant(client, admin_headers, db):
    from sqlalchemy import func

    from app.models.inventory import Batch, StockLedger

    _, batch, sale = a_sale(client, admin_headers)
    client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"],
              "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "6"}],
              "refund_mode": "CASH"},
        headers=admin_headers,
    )

    ledger = client.get(
        f"{API}/inventory/ledger", params={"batch_id": batch["id"]}, headers=admin_headers
    ).json()
    entry = ledger["items"][0]
    assert entry["movement_type"] == "SALE_RETURN"
    assert entry["qty_units_delta"] == "6.000"
    assert entry["reference_type"] == "RETURN_ITEM"

    db.expire_all()
    row = db.get(Batch, batch["id"])
    assert row.qty_available_units == (
        db.query(func.sum(StockLedger.qty_units_delta))
        .filter(StockLedger.batch_id == row.id)
        .scalar()
    )


def test_the_sale_status_follows_what_has_come_back(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)
    item_id = sale["items"][0]["id"]

    client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"], "items": [{"sale_item_id": item_id, "qty_units": "5"}],
              "refund_mode": "CASH"},
        headers=admin_headers,
    )
    assert client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()[
        "status"
    ] == "PARTIALLY_RETURNED"

    client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"], "items": [{"sale_item_id": item_id, "qty_units": "15"}],
              "refund_mode": "CASH"},
        headers=admin_headers,
    )
    assert client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()[
        "status"
    ] == "RETURNED"


def test_nothing_can_be_returned_against_a_void_sale(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request",
        json={"reason": "Entered against the wrong customer"}, headers=admin_headers,
    ).json()
    client.post(f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers)

    response = client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"],
              "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "1"}],
              "refund_mode": "CASH"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SALE_ALREADY_VOIDED"


# --- the return window ----------------------------------------------------


def test_an_old_return_needs_an_admin(client, admin_headers, sales_headers, db):
    """Past the window it is the owner's decision, not the counter's."""
    from app.models.sale import Sale

    _, _, sale = a_sale(client, admin_headers)

    # Age the invoice past the 7-day default.
    row = db.get(Sale, sale["id"])
    row.invoice_date = business_date() - timedelta(days=30)
    db.commit()

    body = {
        "sale_id": sale["id"],
        "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "1"}],
        "refund_mode": "CASH",
    }
    refused = client.post(f"{API}/returns", json=body, headers=sales_headers)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "RETURN_WINDOW_EXCEEDED"

    allowed = client.post(f"{API}/returns", json=body, headers=admin_headers)
    assert allowed.status_code == 201
    assert allowed.json()["approved_by_name"] == "Test Admin"
    assert allowed.json()["days_since_sale"] == 30
    assert any("30 days after the sale" in w for w in allowed.json()["warnings"])


# --- credit notes ---------------------------------------------------------


def test_a_return_issues_a_credit_note_by_default(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)

    body = client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"],
              "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}]},
        headers=admin_headers,
    ).json()

    assert body["refund_mode"] == "CREDIT_NOTE"
    assert body["credit_note_no"] == "00000001"

    note = client.get(
        f"{API}/credit-notes/{body['credit_note_id']}", headers=admin_headers
    ).json()
    assert note["amount_issued"] == "50.00"
    assert note["balance_amount"] == "50.00"
    assert note["status"] == "OPEN"
    assert note["days_to_expiry"] == 180        # the store default


def test_a_walk_in_cannot_be_given_a_credit_note(client, admin_headers):
    """There would be no way to know who is entitled to spend it."""
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "5", discount_pct="0")])

    response = client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"],
              "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "1"}],
              "refund_mode": "CREDIT_NOTE"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_NEEDS_CUSTOMER"


def test_a_cash_refund_issues_no_note(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)

    body = client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"],
              "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}],
              "refund_mode": "CASH"},
        headers=admin_headers,
    ).json()
    assert body["credit_note_id"] is None
    assert client.get(f"{API}/credit-notes", headers=admin_headers).json()["total"] == 0


def test_the_note_shows_against_the_customer(client, admin_headers):
    _, _, sale = a_sale(client, admin_headers)
    client.post(
        f"{API}/returns",
        json={"sale_id": sale["id"],
              "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "5"}]},
        headers=admin_headers,
    )

    found = client.get(
        f"{API}/credit-notes", params={"mobile": "9876500001", "open_only": True},
        headers=admin_headers,
    ).json()
    assert found["total"] == 1

    customer = client.get(
        f"{API}/customers/by-mobile/9876500001", headers=admin_headers
    ).json()
    assert customer["credit_balance"] == "50.00"
