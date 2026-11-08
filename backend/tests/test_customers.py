"""The customer register — who has bought what, and what credit they hold.

The register is read by the counter, so nothing here is admin-gated. What
is tested is that the history attached to a row is *true*: voided invoices
excluded, spend summed exactly, and a lapsed credit note not counted as
money the customer can still spend.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.models.returns import CreditNote
from app.utils.clock import business_date

from tests.conftest import API, MISSING_ID, sell, stocked_medicine

_brand = 0


def a_brand() -> str:
    """Each helper catalogues its own brand — brand names are unique."""
    global _brand
    _brand += 1
    return f"Registryl {_brand}"


def buy(client, headers, mobile, *, units="2", mrp="100.00", name="Reg Tester"):
    """One sale to one customer, returning the invoice."""
    medicine, _ = stocked_medicine(
        client, headers, mrp=mrp, qty_packs="50", brand_name=a_brand()
    )
    return sell(
        client,
        headers,
        [{"medicine_id": medicine["id"], "qty_units": units, "discount_pct": "0"}],
        customer={"mobile": mobile, "name": name},
    )


def row_for(client, headers, mobile):
    page = client.get(f"{API}/customers", params={"q": mobile}, headers=headers).json()
    return next(r for r in page["items"] if r["mobile"] == mobile)


# --- the register ---------------------------------------------------------


def test_the_register_carries_each_customers_history(client, admin_headers):
    mobile = "9000000101"
    buy(client, admin_headers, mobile, units="2", mrp="100.00")   # ₹20
    buy(client, admin_headers, mobile, units="3", mrp="100.00")   # ₹30

    row = row_for(client, admin_headers, mobile)
    assert row["sale_count"] == 2
    assert row["total_spend"] == "50.00"
    assert row["last_purchase_on"] == business_date().isoformat()


def test_a_customer_who_has_never_bought_shows_zero_not_null(client, admin_headers):
    created = client.post(
        f"{API}/customers", json={"mobile": "9000000102", "name": "Walk Past"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    row = row_for(client, admin_headers, "9000000102")
    assert row["sale_count"] == 0
    assert row["total_spend"] == "0.00"
    assert row["last_purchase_on"] is None


def test_a_voided_invoice_leaves_no_trace_in_the_history(client, admin_headers):
    mobile = "9000000103"
    sale = buy(client, admin_headers, mobile, units="4", mrp="100.00")   # ₹40

    before = row_for(client, admin_headers, mobile)
    assert before["total_spend"] == "40.00"

    request = client.post(
        f"{API}/sales/{sale['id']}/void-request",
        json={"reason": "Billed to the wrong person."}, headers=admin_headers,
    ).json()
    approved = client.post(
        f"{API}/void-requests/{request['id']}/approve", json={}, headers=admin_headers
    )
    assert approved.status_code == 200, approved.text

    after = row_for(client, admin_headers, mobile)
    assert after["sale_count"] == 0
    assert after["total_spend"] == "0.00"


def test_the_counter_can_read_the_register(client, sales_headers):
    assert client.get(f"{API}/customers", headers=sales_headers).status_code == 200


def test_the_register_searches_by_name_as_well_as_mobile(client, admin_headers):
    client.post(
        f"{API}/customers", json={"mobile": "9000000104", "name": "Sunita Rao"},
        headers=admin_headers,
    )
    found = client.get(
        f"{API}/customers", params={"q": "sunita"}, headers=admin_headers
    ).json()
    assert any(r["mobile"] == "9000000104" for r in found["items"])


def test_holders_of_credit_can_be_listed_on_their_own(client, admin_headers, db):
    mobile = "9000000105"
    buy(client, admin_headers, mobile)
    customer = row_for(client, admin_headers, mobile)

    plain = client.get(
        f"{API}/customers", params={"has_credit": True}, headers=admin_headers
    ).json()
    assert not any(r["id"] == customer["id"] for r in plain["items"])


# --- one customer's card --------------------------------------------------


def test_the_card_lists_the_customers_invoices(client, admin_headers):
    mobile = "9000000106"
    first = buy(client, admin_headers, mobile, units="2")
    second = buy(client, admin_headers, mobile, units="5")

    customer = row_for(client, admin_headers, mobile)
    card = client.get(f"{API}/customers/{customer['id']}", headers=admin_headers)
    assert card.status_code == 200, card.text
    body = card.json()

    assert body["sale_count"] == 2
    assert body["first_purchase_on"] == business_date().isoformat()
    invoices = [s["invoice_no"] for s in body["recent_sales"]]
    assert invoices == [second["invoice_no"], first["invoice_no"]]   # newest first
    assert body["recent_sales"][0]["item_count"] == 1


def test_the_card_shows_credit_still_spendable(client, admin_headers):
    mobile = "9000000107"
    medicine, _ = stocked_medicine(
        client, admin_headers, mrp="100.00", qty_packs="50", brand_name=a_brand()
    )
    sale = sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "0"}],
        customer={"mobile": mobile, "name": "Credit Holder"},
    )
    returned = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "10"}],
        },
        headers=admin_headers,
    )
    assert returned.status_code == 201, returned.text

    customer = row_for(client, admin_headers, mobile)
    body = client.get(f"{API}/customers/{customer['id']}", headers=admin_headers).json()

    assert body["open_credit"] == "100.00"
    assert body["credit_balance"] == "100.00"      # cached column agrees
    assert len(body["credit_notes"]) == 1
    assert body["credit_notes"][0]["balance_amount"] == "100.00"


def test_a_lapsed_note_is_listed_but_is_not_spendable(client, admin_headers, db):
    mobile = "9000000108"
    medicine, _ = stocked_medicine(
        client, admin_headers, mrp="100.00", qty_packs="50", brand_name=a_brand()
    )
    sale = sell(
        client, admin_headers,
        [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "0"}],
        customer={"mobile": mobile, "name": "Late Returner"},
    )
    issued = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "10"}],
        },
        headers=admin_headers,
    ).json()

    note = db.get(CreditNote, issued["credit_note_id"])
    note.expires_on = business_date() - timedelta(days=1)
    db.commit()

    customer = row_for(client, admin_headers, mobile)
    body = client.get(f"{API}/customers/{customer['id']}", headers=admin_headers).json()

    assert body["open_credit"] == "0.00"           # nothing left to spend
    assert len(body["credit_notes"]) == 1          # but the history stands
    assert body["credit_notes"][0]["days_to_expiry"] == -1


def test_a_card_from_another_store_is_not_found(client, admin_headers):
    missing = client.get(f"{API}/customers/{MISSING_ID}", headers=admin_headers)
    assert missing.status_code == 404


def test_the_notes_field_survives_a_round_trip(client, admin_headers):
    created = client.post(
        f"{API}/customers",
        json={"mobile": "9000000109", "name": "Noted", "notes": "Allergic to sulfa."},
        headers=admin_headers,
    ).json()
    assert created["notes"] == "Allergic to sulfa."

    card = client.get(f"{API}/customers/{created['id']}", headers=admin_headers).json()
    assert card["notes"] == "Allergic to sulfa."


def test_spend_is_summed_as_a_decimal_not_a_scaled_integer(client, admin_headers):
    """SQLite holds money as a scaled integer (₹33.33 -> 3333).

    A ``SUM()`` that skipped the type's result processor would come back a
    hundredfold too large and nobody would notice until a customer's card
    claimed lakhs. The total must equal the invoices it is made of.
    """
    mobile = "9000000110"
    medicine, _ = stocked_medicine(
        client, admin_headers, mrp="33.33", qty_packs="50", brand_name=a_brand()
    )
    billed = [
        Decimal(
            sell(
                client, admin_headers,
                [{"medicine_id": medicine["id"], "qty_units": "10", "discount_pct": "0"}],
                customer={"mobile": mobile},
            )["net_amount"]
        )
        for _ in range(3)
    ]

    row = row_for(client, admin_headers, mobile)
    assert Decimal(row["total_spend"]) == sum(billed)
    assert "." in row["total_spend"]   # a Decimal came back, not a raw count
