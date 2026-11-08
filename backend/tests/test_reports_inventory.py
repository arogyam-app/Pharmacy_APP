"""Movement and valuation reports.

The valuation numbers are checked against products worked by hand, because
a quantity times a price is exactly the arithmetic that goes wrong when a
scaled-integer column is multiplied in SQL rather than in Python.
"""

from datetime import timedelta

from app.utils.clock import business_date
from tests.conftest import API, make_medicine, make_purchase, make_supplier, sell, stocked_medicine

TODAY = business_date()


def fetch(client, headers, path, **params):
    query = {"date_from": TODAY.isoformat(), "date_to": TODAY.isoformat(), **params}
    response = client.get(f"{API}/reports{path}", params=query, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def rows_by(body, key):
    return {row[key]: row for row in body["rows"]}


def a_batch(client, headers, medicine_id, **overrides):
    payload = {
        "medicine_id": medicine_id,
        "batch_no": "B1",
        "expiry_date": (TODAY + timedelta(days=365)).isoformat(),
        "qty_packs": "10",
        "mrp_per_pack": "100.00",
        "gst_rate": "12",
    }
    payload.update(overrides)
    response = client.post(f"{API}/inventory/batches", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --- fast moving ---------------------------------------------------------


def test_fast_moving_ranks_by_units_and_reports_cover(client, admin_headers):
    slow, _ = stocked_medicine(client, admin_headers, brand_name="Slowol", qty_packs="10")
    fast, _ = stocked_medicine(client, admin_headers, brand_name="Fastol", qty_packs="10")

    sell(client, admin_headers, [{"medicine_id": fast["id"], "qty_units": "50"}])
    sell(client, admin_headers, [{"medicine_id": slow["id"], "qty_units": "5"}])

    body = fetch(client, admin_headers, "/inventory/fast-moving")

    assert [row["medicine"] for row in body["rows"]] == ["Fastol", "Slowol"]
    top = body["rows"][0]
    assert top["units_sold"] == "50.000"
    assert top["stock_units"] == "50.000"          # 100 units in, 50 out
    assert top["turnover_ratio"] == "1.00"         # sold as much as is left
    assert top["days_of_cover"] == 1               # a one-day window, 50/day
    assert body["totals"]["units_sold"] == "55.000"


def test_fast_moving_honours_the_top_limit(client, admin_headers):
    for index in range(3):
        medicine, _ = stocked_medicine(client, admin_headers, brand_name=f"Topol {index}")
        sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": str(index + 1)}])

    body = fetch(client, admin_headers, "/inventory/fast-moving", top=2)

    assert len(body["rows"]) == 2
    assert body["meta"]["top"] == 2


def test_a_medicine_that_never_sold_is_absent_from_fast_moving(client, admin_headers):
    stocked_medicine(client, admin_headers, brand_name="Neverol")

    assert fetch(client, admin_headers, "/inventory/fast-moving")["rows"] == []


# --- slow moving ---------------------------------------------------------


def test_slow_moving_lists_stock_that_did_not_move_with_its_locked_value(
    client, admin_headers
):
    supplier = make_supplier(client, admin_headers)
    idle = make_medicine(client, admin_headers, brand_name="Idlol")
    moving = make_medicine(client, admin_headers, brand_name="Movol")
    # 10 packs of 10 at ₹100 cost, ₹150 MRP -> 100 units at ₹10 / ₹15.
    make_purchase(client, admin_headers, supplier["id"], idle["id"])
    make_purchase(
        client, admin_headers, supplier["id"], moving["id"],
        supplier_invoice_no="SUP-002",
    )
    sell(client, admin_headers, [{"medicine_id": moving["id"], "qty_units": "5"}])

    body = fetch(client, admin_headers, "/inventory/slow-moving")

    assert [row["medicine"] for row in body["rows"]] == ["Idlol"]
    row = body["rows"][0]
    assert row["units_sold"] == "0.000"
    assert row["stock_units"] == "100.000"
    assert row["cost_value"] == "1000.00"
    assert row["mrp_value"] == "1500.00"
    assert row["last_sold"] == ""
    assert body["meta"]["threshold_units"] == "0.000"


def test_the_threshold_widens_what_counts_as_slow(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Trickleol")
    sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": "3"}])

    assert fetch(client, admin_headers, "/inventory/slow-moving")["rows"] == []

    body = fetch(client, admin_headers, "/inventory/slow-moving", threshold_units="5")
    assert body["rows"][0]["medicine"] == "Trickleol"
    assert body["rows"][0]["units_sold"] == "3.000"
    assert body["rows"][0]["last_sold"] == TODAY.isoformat()
    assert body["rows"][0]["days_since_sale"] == 0


def test_a_medicine_with_no_stock_is_not_a_slow_mover(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Soldoutol", qty_packs="1")
    sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": "10"}])

    body = fetch(client, admin_headers, "/inventory/slow-moving", threshold_units="100")
    assert body["rows"] == []


# --- valuation -----------------------------------------------------------


def test_valuation_multiplies_units_by_cost_and_by_mrp(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers, brand_name="Valuol")
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    body = fetch(client, admin_headers, "/inventory/valuation")

    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["medicine"] == "Valuol"
    assert row["stock_units"] == "100.000"
    assert row["unit_cost"] == "10.00"
    assert row["cost_value"] == "1000.00"
    assert row["unit_mrp"] == "15.00"
    assert row["mrp_value"] == "1500.00"
    assert row["margin_value"] == "500.00"
    assert row["margin_pct"] == "33.33"
    assert body["totals"]["cost_value"] == "1000.00"
    assert body["meta"]["as_at"] == TODAY.isoformat()


def test_opening_stock_has_no_cost_and_is_counted_as_such(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Openingol")

    body = fetch(client, admin_headers, "/inventory/valuation")

    assert body["rows"][0]["unit_cost"] == ""
    assert body["rows"][0]["cost_value"] == "0.00"
    assert body["meta"]["batches_without_cost"] == 1


def test_an_empty_batch_leaves_the_valuation(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Emptyol", qty_packs="1")
    sell(client, admin_headers, [{"medicine_id": medicine["id"], "qty_units": "10"}])

    assert fetch(client, admin_headers, "/inventory/valuation")["rows"] == []


# --- expiry ageing -------------------------------------------------------


def test_expiry_ageing_puts_each_batch_in_its_bucket(client, admin_headers):
    near = make_medicine(client, admin_headers, brand_name="Nearol")
    far = make_medicine(client, admin_headers, brand_name="Farol")
    a_batch(client, admin_headers, near["id"], expiry_date=(TODAY + timedelta(days=20)).isoformat())
    a_batch(
        client, admin_headers, far["id"], batch_no="B2",
        expiry_date=(TODAY + timedelta(days=200)).isoformat(),
    )

    body = fetch(client, admin_headers, "/inventory/expiry-ageing")
    buckets = rows_by(body, "bucket")

    # Every bucket is present, even the empty ones — the five time windows,
    # "Expired", and the one for stock with no shelf life at all.
    assert len(body["rows"]) == 7
    assert buckets["Within 30 days"]["batch_count"] == 1
    assert buckets["Within 30 days"]["stock_units"] == "100.000"
    assert buckets["Within 30 days"]["mrp_value"] == "1000.00"
    assert buckets["Beyond 180 days"]["batch_count"] == 1
    assert buckets["31 - 60 days"]["batch_count"] == 0
    assert buckets["Within 30 days"]["share_pct"] == "50.00"
    assert body["totals"]["mrp_value"] == "2000.00"
    assert body["totals"]["share_pct"] == "100.00"


def test_an_expired_batch_lands_in_the_expired_bucket(client, admin_headers, db):
    medicine, batch = stocked_medicine(client, admin_headers, brand_name="Goneol")

    from app.models.inventory import Batch

    row = db.query(Batch).filter(Batch.id == batch["id"]).one()
    row.expiry_date = TODAY - timedelta(days=1)
    db.commit()

    buckets = rows_by(fetch(client, admin_headers, "/inventory/expiry-ageing"), "bucket")
    assert buckets["Expired"]["batch_count"] == 1
    assert buckets["Expired"]["medicine_count"] == 1


def test_ageing_of_an_empty_shelf_is_all_zeroes(client, admin_headers):
    body = fetch(client, admin_headers, "/inventory/expiry-ageing")

    assert len(body["rows"]) == 7
    assert all(row["batch_count"] == 0 for row in body["rows"])
    assert body["totals"]["share_pct"] == "0.00"


# --- access --------------------------------------------------------------


def test_a_sales_executive_cannot_read_stock_valuation(client, sales_headers):
    response = client.get(f"{API}/reports/inventory/valuation", headers=sales_headers)
    assert response.status_code == 403
