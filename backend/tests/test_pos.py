"""Pricing, FEFO allocation and the discount policy.

These are the calculations a pharmacy is audited on, so they are tested
against worked examples rather than against the code that produces them.
"""

from app.utils.clock import business_date
from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import API, MISSING_ID, make_batch, make_medicine, stocked_medicine


def quote(client, headers, items, **extra):
    return client.post(f"{API}/pos/quote", json={"items": items, **extra}, headers=headers)


def line_of(medicine_id, qty="1", **kw):
    return {"medicine_id": medicine_id, "qty_units": qty, **kw}


# --- the price ------------------------------------------------------------


def test_mrp_is_gst_inclusive_and_tax_is_carved_out(client, admin_headers):
    """₹100 a strip of ten, one tablet, 12 % GST, no discount."""
    medicine, _ = stocked_medicine(client, admin_headers, mrp="100.00")

    body = quote(
        client, admin_headers, [line_of(medicine["id"], "1", discount_pct="0")]
    ).json()
    line = body["lines"][0]

    assert line["unit_mrp"] == "10.0000"      # 100 / 10 tablets
    assert line["gross_amount"] == "10.00"
    assert line["net_amount"] == "10.00"      # the customer pays the printed price
    assert line["taxable_value"] == "8.93"    # 10 / 1.12
    assert Decimal(line["cgst_amount"]) + Decimal(line["sgst_amount"]) == Decimal("1.07")
    assert line["igst_amount"] == "0.00"


def test_a_fractional_quantity_is_refused(client, admin_headers):
    """Half a tablet is not something the counter can hand over."""
    medicine, _ = stocked_medicine(client, admin_headers)

    response = quote(client, admin_headers, [line_of(medicine["id"], "1.5")])
    assert response.status_code == 422
    assert "whole number" in response.json()["detail"]["message"]


def test_a_single_tablet_can_be_sold(client, admin_headers):
    """The whole point of tracking stock in units rather than packs."""
    medicine, _ = stocked_medicine(client, admin_headers, mrp="100.00", qty_packs="1")

    body = quote(client, admin_headers, [line_of(medicine["id"], "1")]).json()
    assert body["lines"][0]["qty_units"] == "1.000"
    assert body["lines"][0]["qty_packs"] == "0.100"


def test_discount_reduces_the_price_and_the_tax_with_it(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, mrp="100.00")

    body = quote(
        client, admin_headers, [line_of(medicine["id"], "10", discount_pct="10")]
    ).json()
    line = body["lines"][0]

    assert line["gross_amount"] == "100.00"
    assert line["discount_amount"] == "10.00"
    assert line["net_amount"] == "90.00"
    assert line["taxable_value"] == "80.36"   # 90 / 1.12
    assert Decimal(line["cgst_amount"]) + Decimal(line["sgst_amount"]) == Decimal("9.64")


def test_line_totals_reconcile_with_the_invoice_totals(client, admin_headers):
    first, _ = stocked_medicine(client, admin_headers, mrp="100.00")
    second, _ = stocked_medicine(
        client, admin_headers, mrp="55.50", brand_name="Testolol 25"
    )

    body = quote(
        client,
        admin_headers,
        [line_of(first["id"], "7"), line_of(second["id"], "3")],
    ).json()
    totals, lines = body["totals"], body["lines"]

    assert sum(Decimal(l["taxable_value"]) for l in lines) == Decimal(totals["taxable_amount"])
    assert sum(Decimal(l["net_amount"]) for l in lines) == Decimal(totals["sub_total"])
    assert Decimal(totals["net_amount"]) == Decimal(totals["sub_total"]) + Decimal(
        totals["round_off"]
    )


def test_rounding_happens_once_on_the_invoice_not_per_line(client, admin_headers):
    """Rounding each line would drift the tax break-up away from the total."""
    medicine, _ = stocked_medicine(client, admin_headers, mrp="99.99")

    body = quote(client, admin_headers, [line_of(medicine["id"], "3")]).json()
    totals = body["totals"]

    assert Decimal(totals["net_amount"]) == Decimal(totals["net_amount"]).quantize(Decimal("1"))
    assert abs(Decimal(totals["round_off"])) <= Decimal("0.50")
    assert Decimal(totals["sub_total"]) + Decimal(totals["round_off"]) == Decimal(
        totals["net_amount"]
    )


def test_amount_in_words_is_indian_numbering(client, admin_headers):
    medicine, _ = stocked_medicine(
        client, admin_headers, mrp="100.00", qty_packs="200"
    )
    body = quote(
        client, admin_headers, [line_of(medicine["id"], "1500", discount_pct="0")]
    ).json()
    # 1500 tablets at ₹10 = ₹15,000 → "fifteen thousand", not "15 thousand"
    assert "fifteen thousand" in body["amount_in_words"].lower()


# --- allocation -----------------------------------------------------------


def test_fefo_picks_the_batch_that_expires_first(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"],
        batch_no="LATE", expiry_date=(business_date() + timedelta(days=700)).isoformat(),
        mrp_per_pack="120.00",
    )
    near = make_batch(
        client, admin_headers, medicine["id"],
        batch_no="SOON", expiry_date=(business_date() + timedelta(days=120)).isoformat(),
        mrp_per_pack="100.00",
    )

    body = quote(client, admin_headers, [line_of(medicine["id"], "5")]).json()
    assert body["lines"][0]["batch_id"] == near["id"]
    assert body["lines"][0]["batch_no"] == "SOON"


def test_a_line_splits_across_batches_when_one_cannot_cover_it(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="A",
        expiry_date=(business_date() + timedelta(days=100)).isoformat(), qty_packs="1",
    )
    make_batch(
        client, admin_headers, medicine["id"], batch_no="B",
        expiry_date=(business_date() + timedelta(days=300)).isoformat(), qty_packs="5",
    )

    body = quote(client, admin_headers, [line_of(medicine["id"], "25")]).json()
    assert len(body["lines"]) == 2
    assert body["lines"][0]["batch_no"] == "A"
    assert body["lines"][0]["qty_units"] == "10.000"     # the whole first batch
    assert body["lines"][1]["qty_units"] == "15.000"
    assert any("different expiry" in w for w in body["warnings"])


def test_the_operator_can_pin_a_specific_batch(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="SOON",
        expiry_date=(business_date() + timedelta(days=100)).isoformat(),
    )
    later = make_batch(
        client, admin_headers, medicine["id"], batch_no="LATE",
        expiry_date=(business_date() + timedelta(days=600)).isoformat(),
    )

    body = quote(
        client, admin_headers, [line_of(medicine["id"], "5", batch_id=later["id"])]
    ).json()
    assert body["lines"][0]["batch_no"] == "LATE"


def test_an_expired_batch_can_never_be_sold(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    expired = make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD",
        expiry_date=(business_date() - timedelta(days=1)).isoformat(),
    )

    # not offered by FEFO...
    fefo = quote(client, admin_headers, [line_of(medicine["id"], "1")])
    assert fefo.status_code == 400
    assert fefo.json()["detail"]["code"] == "INSUFFICIENT_STOCK"
    assert "expired" in fefo.json()["detail"]["message"]

    # ...and refused even when asked for by name.
    pinned = quote(
        client, admin_headers, [line_of(medicine["id"], "1", batch_id=expired["id"])]
    )
    assert pinned.status_code == 400
    assert pinned.json()["detail"]["code"] == "BATCH_EXPIRED"


def test_a_blocked_batch_is_out_of_circulation(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    batch = make_batch(client, admin_headers, medicine["id"])
    client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"is_blocked": True, "block_reason": "Recall"},
        headers=admin_headers,
    )

    pinned = quote(
        client, admin_headers, [line_of(medicine["id"], "1", batch_id=batch["id"])]
    )
    assert pinned.status_code == 400
    assert pinned.json()["detail"]["code"] == "BATCH_BLOCKED"
    assert "Recall" in pinned.json()["detail"]["message"]


def test_overselling_is_refused_with_the_real_figure(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="2")  # 20 tablets

    response = quote(client, admin_headers, [line_of(medicine["id"], "25")])
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "INSUFFICIENT_STOCK"
    assert "20" in detail["message"] and "25" in detail["message"]


def test_near_expiry_stock_warns_but_still_sells(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="NEAR",
        expiry_date=(business_date() + timedelta(days=20)).isoformat(),
    )

    body = quote(client, admin_headers, [line_of(medicine["id"], "1")]).json()
    assert body["lines"][0]["is_near_expiry"] is True
    assert any("expires on" in w for w in body["warnings"])


# --- discount policy ------------------------------------------------------


def test_the_batch_discount_limit_overrides_the_store_policy(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"],
        default_discount_pct="15", max_discount_pct="25",
    )

    body = quote(client, admin_headers, [line_of(medicine["id"], "10")]).json()
    line = body["lines"][0]
    assert line["discount_pct"] == "15.00"     # the batch's suggestion, not the store's 10
    assert line["max_discount_pct"] == "25.00"


def test_the_suggested_discount_is_clamped_to_the_ceiling(client, admin_headers):
    """A store default of 10 % must not be offered on a lot capped at 5 %."""
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"], max_discount_pct="5")

    body = quote(client, admin_headers, [line_of(medicine["id"], "10")]).json()
    assert body["lines"][0]["discount_pct"] == "5.00"


def test_a_sales_exec_cannot_exceed_the_discount_limit(client, admin_headers, sales_headers):
    medicine, _ = stocked_medicine(client, admin_headers)   # store max is 20 %

    response = quote(
        client, sales_headers, [line_of(medicine["id"], "10", discount_pct="35")]
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "DISCOUNT_LIMIT_EXCEEDED"
    assert detail["field"] == "items.0.discount_pct"


def test_an_admin_override_needs_a_reason(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)

    without = quote(
        client, admin_headers, [line_of(medicine["id"], "10", discount_pct="35")]
    )
    assert without.status_code == 400
    assert without.json()["detail"]["code"] == "DISCOUNT_OVERRIDE_REASON_REQUIRED"

    with_reason = quote(
        client,
        admin_headers,
        [line_of(medicine["id"], "10", discount_pct="35")],
        discount_override_reason="Damaged carton, agreed with the customer",
    )
    assert with_reason.status_code == 200
    assert with_reason.json()["lines"][0]["discount_pct"] == "35.00"


def test_a_sales_exec_cannot_override_even_with_a_reason(client, admin_headers, sales_headers):
    """The reason field is not a way round the policy for a non-admin."""
    medicine, _ = stocked_medicine(client, admin_headers)

    response = quote(
        client,
        sales_headers,
        [line_of(medicine["id"], "10", discount_pct="35")],
        discount_override_reason="Trying it on",
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "DISCOUNT_LIMIT_EXCEEDED"


# --- prescriptions --------------------------------------------------------


def test_a_scheduled_drug_needs_a_doctor(client, admin_headers):
    medicine, _ = stocked_medicine(
        client, admin_headers, brand_name="Alprax 0.5", schedule_type="SCHEDULE_H1"
    )

    response = quote(client, admin_headers, [line_of(medicine["id"], "1")])
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "PRESCRIPTION_REQUIRED"
    assert detail["field"] == "doctor_id"


def test_naming_a_doctor_lets_a_scheduled_drug_through(client, admin_headers):
    medicine, _ = stocked_medicine(
        client, admin_headers, brand_name="Alprax 0.5", schedule_type="SCHEDULE_H1"
    )
    doctor = client.post(
        f"{API}/doctors", json={"name": "Dr A Sharma"}, headers=admin_headers
    ).json()

    body = quote(
        client, admin_headers, [line_of(medicine["id"], "1")], doctor_id=doctor["id"]
    )
    assert body.status_code == 200
    assert body.json()["requires_doctor"] is True


# --- quoting writes nothing ----------------------------------------------


def test_quoting_never_moves_stock(client, admin_headers, db):
    from app.models.inventory import Batch

    medicine, batch = stocked_medicine(client, admin_headers)
    before = db.get(Batch, batch["id"]).qty_available_units

    for _ in range(3):
        quote(client, admin_headers, [line_of(medicine["id"], "5")])

    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == before


def test_a_stale_reference_is_reported_against_its_own_field(client, admin_headers):
    """The POS cart survives a reload, so it can outlive the rows it names.

    The field on the error is what lets the client drop just that reference
    and retry, instead of the whole cart becoming unusable.
    """
    medicine, _ = stocked_medicine(client, admin_headers)

    stale_customer = quote(
        client, admin_headers, [line_of(medicine["id"], "1")],
        customer={"customer_id": MISSING_ID},
    )
    assert stale_customer.status_code == 404
    assert stale_customer.json()["detail"]["field"] == "customer_id"

    stale_doctor = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "1", discount_pct="0")],
            "doctor_id": MISSING_ID,
            "payments": [{"mode": "CASH", "amount": "10.00"}],
        },
        headers=admin_headers,
    )
    assert stale_doctor.status_code == 404
    assert stale_doctor.json()["detail"]["field"] == "doctor_id"


def test_out_of_stock_medicines_are_still_listed_but_cannot_be_sold(client, admin_headers):
    """The counter needs to see that the shop carries the brand at all —
    that is how the operator knows to offer an alternative."""
    medicine = make_medicine(client, admin_headers)     # no batch at all

    results = client.get(
        f"{API}/medicines/search", params={"q": "Testolol"}, headers=admin_headers
    ).json()
    listed = [m for m in results if m["id"] == medicine["id"]]
    assert listed, "an unstocked medicine should still appear in the search"
    assert listed[0]["available_units"] == "0.000"
    assert listed[0]["mrp_per_pack"] is None

    response = quote(client, admin_headers, [line_of(medicine["id"], "1")])
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INSUFFICIENT_STOCK"


def test_quoting_an_unknown_mobile_does_not_create_a_customer(client, admin_headers, db):
    from app.models.party import Customer

    medicine, _ = stocked_medicine(client, admin_headers)
    quote(
        client,
        admin_headers,
        [line_of(medicine["id"], "1")],
        customer={"mobile": "9876500000", "name": "Passer By"},
    )
    assert db.query(Customer).count() == 0
