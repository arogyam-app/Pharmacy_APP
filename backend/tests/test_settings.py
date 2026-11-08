"""Store settings: the discount policy every later phase reads from."""

from decimal import Decimal

from app.models.audit import AuditLog
from app.models.store import StoreSettings
from tests.conftest import API


def test_seeded_defaults_match_the_spec(client, sales_headers):
    body = client.get(f"{API}/settings", headers=sales_headers).json()

    assert Decimal(body["default_discount_pct"]) == Decimal("10.00")
    assert Decimal(body["max_discount_pct"]) == Decimal("20.00")
    assert body["near_expiry_days"] == 90
    assert Decimal(body["order_book_decline_pct"]) == Decimal("60.00")
    assert body["return_window_days"] == 7
    assert body["credit_note_validity_days"] == 180
    assert body["gst_enabled"] is True
    assert body["price_includes_gst"] is True


def test_sales_executive_can_read_but_not_write_settings(client, sales_headers):
    assert client.get(f"{API}/settings", headers=sales_headers).status_code == 200
    assert client.patch(
        f"{API}/settings", json={"max_discount_pct": "80"}, headers=sales_headers
    ).status_code == 403


def test_admin_updates_the_discount_policy(client, admin_headers, db, store):
    response = client.patch(
        f"{API}/settings",
        json={"default_discount_pct": "5", "max_discount_pct": "25"},
        headers=admin_headers,
    )
    assert response.status_code == 200

    db.expire_all()
    row = db.query(StoreSettings).filter(StoreSettings.store_id == store.id).one()
    assert row.default_discount_pct == Decimal("5.00")
    assert row.max_discount_pct == Decimal("25.00")


def test_decimal_settings_survive_the_round_trip_exactly(client, admin_headers, db, store):
    client.patch(f"{API}/settings", json={"max_discount_pct": "17.35"}, headers=admin_headers)

    db.expire_all()
    row = db.query(StoreSettings).filter(StoreSettings.store_id == store.id).one()
    assert row.max_discount_pct == Decimal("17.35")


def test_default_above_max_is_rejected_even_in_a_partial_update(client, admin_headers):
    # max stays at the stored 20 %, so a 30 % default must fail.
    response = client.patch(
        f"{API}/settings", json={"default_discount_pct": "30"}, headers=admin_headers
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "DISCOUNT_POLICY_INVALID"


def test_both_fields_supplied_inconsistently_is_rejected(client, admin_headers):
    response = client.patch(
        f"{API}/settings",
        json={"default_discount_pct": "40", "max_discount_pct": "20"},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_out_of_range_discount_is_rejected(client, admin_headers):
    assert client.patch(
        f"{API}/settings", json={"max_discount_pct": "140"}, headers=admin_headers
    ).status_code == 422
    assert client.patch(
        f"{API}/settings", json={"max_discount_pct": "-5"}, headers=admin_headers
    ).status_code == 422


def test_settings_change_is_audited_with_before_and_after(client, admin_headers, db):
    client.patch(f"{API}/settings", json={"max_discount_pct": "22.5"}, headers=admin_headers)

    entry = db.query(AuditLog).filter(AuditLog.entity_type == "StoreSettings").one()
    assert entry.action == "UPDATE"
    assert "20.00" in entry.before_json
    assert "22.50" in entry.after_json


def test_store_profile_is_readable_by_all_and_writable_by_admin(client, admin_headers, sales_headers):
    assert client.get(f"{API}/store", headers=sales_headers).status_code == 200

    response = client.patch(
        f"{API}/store",
        json={"gstin": "18AABCU9603R1ZM", "state_code": "18", "city": "Guwahati"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["gstin"] == "18AABCU9603R1ZM"


def test_invalid_state_code_is_rejected(client, admin_headers):
    response = client.patch(f"{API}/store", json={"state_code": "ASSAM"}, headers=admin_headers)

    assert response.status_code == 422


def test_the_store_email_round_trips(client, admin_headers):
    """Saved on the profile, and readable again — the bill prints it beside
    the phone number, so the two have to survive the same PATCH."""
    response = client.patch(
        f"{API}/store",
        json={"phone": "9435012345", "email": "counter@arogyam.example"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["email"] == "counter@arogyam.example"

    fetched = client.get(f"{API}/store", headers=admin_headers).json()
    assert fetched["phone"] == "9435012345"
    assert fetched["email"] == "counter@arogyam.example"


def test_the_store_email_can_be_cleared(client, admin_headers):
    """The form submits every field, so a blank box arrives as null."""
    client.patch(f"{API}/store", json={"email": "counter@arogyam.example"}, headers=admin_headers)

    response = client.patch(f"{API}/store", json={"email": None}, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.json()["email"] is None


def test_a_store_email_that_is_not_an_address_is_rejected(client, admin_headers):
    response = client.patch(f"{API}/store", json={"email": "counter at shop"}, headers=admin_headers)

    assert response.status_code == 422


# --- A5_new: a second A5 to work on --------------------------------------


def test_a5_new_can_be_set_as_the_invoice_format(client, admin_headers):
    """A copy of A5, so one can be changed without disturbing the other."""
    response = client.patch(
        f"{API}/settings", json={"print_format": "A5_NEW"}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["print_format"] == "A5_NEW"


def test_the_bill_reports_a5_new_as_its_format(client, admin_headers):
    """The invoice screen picks its layout from this, so it has to arrive."""
    from tests.conftest import make_batch, make_medicine

    client.patch(f"{API}/settings", json={"print_format": "A5_NEW"}, headers=admin_headers)

    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"])
    items = [{"medicine_id": medicine["id"], "qty_units": "1"}]
    quote = client.post(f"{API}/pos/quote", json={"items": items}, headers=admin_headers).json()
    sale = client.post(
        f"{API}/sales",
        json={
            "items": items,
            "payments": [{"mode": "CASH", "amount": quote["totals"]["net_amount"]}],
        },
        headers=admin_headers,
    ).json()

    payload = client.get(f"{API}/sales/{sale['id']}/print", headers=admin_headers).json()
    assert payload["print_format"] == "A5_NEW"


def test_the_bill_carries_the_store_phone_and_email(client, admin_headers):
    """A5_new prints the two side by side in the header. They are saved on
    the store profile, two screens away from the bill that shows them."""
    from tests.conftest import make_batch, make_medicine

    client.patch(
        f"{API}/store",
        json={"phone": "9435012345", "email": "counter@arogyam.example"},
        headers=admin_headers,
    )

    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"])
    items = [{"medicine_id": medicine["id"], "qty_units": "1"}]
    quote = client.post(f"{API}/pos/quote", json={"items": items}, headers=admin_headers).json()
    sale = client.post(
        f"{API}/sales",
        json={
            "items": items,
            "payments": [{"mode": "CASH", "amount": quote["totals"]["net_amount"]}],
        },
        headers=admin_headers,
    ).json()

    store = client.get(f"{API}/sales/{sale['id']}/print", headers=admin_headers).json()["store"]
    assert store["phone"] == "9435012345"
    assert store["email"] == "counter@arogyam.example"


def test_an_unknown_invoice_format_is_still_refused(client, admin_headers):
    response = client.patch(
        f"{API}/settings", json={"print_format": "A3_FANCY"}, headers=admin_headers
    )
    assert response.status_code == 422
