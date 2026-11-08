"""Alerts, the expiry register and the order book.

The phase's acceptance criterion: selling past the 60 % threshold raises an
`OPEN` order-book entry with a sensible quantity, and receiving a purchase
for it closes the entry and resets the baseline.
"""

from app.utils.clock import business_date
from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import (
    API,
    make_batch,
    make_medicine,
    make_purchase,
    make_supplier,
    sell,
    stocked_medicine,
)


def line_of(medicine_id, qty="1", **kw):
    return {"medicine_id": medicine_id, "qty_units": qty, **kw}


_seq = iter(range(1, 200))


def a_medicine(client, headers, **kw):
    """Its own brand, so tests can hold several without colliding."""
    kw.setdefault("brand_name", f"Watchol {next(_seq)}")
    return kw.pop("brand_name"), kw


# --- the alert buckets ----------------------------------------------------


def test_the_summary_counts_every_bucket(client, admin_headers):
    fresh, _ = stocked_medicine(client, admin_headers, brand_name="Freshol")

    expired_med = make_medicine(client, admin_headers, brand_name="Oldol")
    make_batch(
        client, admin_headers, expired_med["id"], batch_no="OLD",
        expiry_date=(business_date() - timedelta(days=5)).isoformat(),
    )

    soon = make_medicine(client, admin_headers, brand_name="Soonol")
    make_batch(
        client, admin_headers, soon["id"], batch_no="SOON",
        expiry_date=(business_date() + timedelta(days=20)).isoformat(),
    )

    # Catalogued but never stocked.
    make_medicine(client, admin_headers, brand_name="Emptyol")

    body = client.get(f"{API}/alerts/summary", headers=admin_headers).json()
    assert body["expired"] == 1
    assert body["near_expiry"] == 1
    # Two: the medicine never stocked, and the one whose only stock has
    # expired — out-of-date boxes are not sellable stock.
    assert body["out_of_stock"] == 2
    assert body["near_expiry_days"] == 90
    assert Decimal(body["expired_value"]) > 0


def test_expired_stock_does_not_count_as_available(client, admin_headers):
    """A shelf of out-of-date boxes is not stock."""
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD", qty_packs="10",
        expiry_date=(business_date() - timedelta(days=1)).isoformat(),
    )

    out = client.get(f"{API}/alerts/out-of-stock", headers=admin_headers).json()
    assert any(row["medicine_id"] == medicine["id"] for row in out)


def test_low_stock_uses_the_medicines_own_reorder_level(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="2")   # 20 units
    client.patch(
        f"{API}/medicines/{medicine['id']}", json={"reorder_level_units": "25"},
        headers=admin_headers,
    )

    low = client.get(f"{API}/alerts/low-stock", headers=admin_headers).json()
    row = [r for r in low if r["medicine_id"] == medicine["id"]]
    assert row, "20 units against a level of 25 should be low"
    assert row[0]["reorder_level_units"] == "25.000"


def test_near_expiry_stock_is_still_sellable(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="SOON",
        expiry_date=(business_date() + timedelta(days=10)).isoformat(),
    )

    expiring = client.get(f"{API}/alerts/expiring", headers=admin_headers).json()
    assert len(expiring) == 1
    assert expiring[0]["days_to_expiry"] == 10

    # And it can be sold — the window is a warning, not a block.
    assert sell(client, admin_headers, [line_of(medicine["id"], "1")])["invoice_no"]


def test_the_counter_sees_alerts_but_not_their_cost(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD", cost_per_pack="60.00",
        expiry_date=(business_date() - timedelta(days=3)).isoformat(),
    )

    as_admin = client.get(f"{API}/alerts/expired", headers=admin_headers).json()[0]
    assert as_admin["cost_value"] is not None

    as_sales = client.get(f"{API}/alerts/expired", headers=sales_headers).json()[0]
    assert "cost_value" not in as_sales
    assert as_sales["mrp_value"] is not None


# --- the expiry register --------------------------------------------------


def test_the_scan_pulls_expired_batches_in_and_is_repeatable(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD", qty_packs="4",
        expiry_date=(business_date() - timedelta(days=9)).isoformat(),
    )

    first = client.post(f"{API}/expiry-register/scan", headers=admin_headers).json()
    assert first["added"] == 1

    second = client.post(f"{API}/expiry-register/scan", headers=admin_headers).json()
    assert second["added"] == 0          # no duplicate row

    listing = client.get(f"{API}/expiry-register", headers=admin_headers).json()
    assert listing["total"] == 1
    entry = listing["items"][0]
    assert entry["status"] == "IDENTIFIED"
    assert entry["days_expired"] == 9
    assert entry["qty_units"] == "40.000"


def test_writing_off_zeroes_the_stock_through_the_ledger(client, admin_headers, db):
    from app.models.inventory import Batch

    medicine = make_medicine(client, admin_headers)
    batch = make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD", qty_packs="3",
        expiry_date=(business_date() - timedelta(days=2)).isoformat(),
    )
    client.post(f"{API}/expiry-register/scan", headers=admin_headers)
    entry = client.get(f"{API}/expiry-register", headers=admin_headers).json()["items"][0]

    response = client.post(
        f"{API}/expiry-register/{entry['id']}/write-off",
        json={"notes": "Binned"}, headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "WRITTEN_OFF"
    assert response.json()["actioned_by_name"] == "Test Admin"

    db.expire_all()
    row = db.get(Batch, batch["id"])
    assert row.qty_available_units == Decimal("0.000")
    assert row.is_blocked is True        # cannot be adjusted back onto the shelf

    ledger = client.get(
        f"{API}/inventory/ledger", params={"batch_id": batch["id"]}, headers=admin_headers
    ).json()
    assert ledger["items"][0]["movement_type"] == "EXPIRY_WRITE_OFF"


def test_the_ledger_invariant_survives_a_write_off(client, admin_headers, db):
    from sqlalchemy import func

    from app.models.inventory import Batch, StockLedger

    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD",
        expiry_date=(business_date() - timedelta(days=2)).isoformat(),
    )
    client.post(f"{API}/expiry-register/scan", headers=admin_headers)
    entry = client.get(f"{API}/expiry-register", headers=admin_headers).json()["items"][0]
    client.post(f"{API}/expiry-register/{entry['id']}/write-off", json={}, headers=admin_headers)

    db.expire_all()
    for batch in db.query(Batch).all():
        assert batch.qty_available_units == (
            db.query(func.sum(StockLedger.qty_units_delta))
            .filter(StockLedger.batch_id == batch.id)
            .scalar()
        )


def test_returning_expired_stock_needs_a_debit_note(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD",
        expiry_date=(business_date() - timedelta(days=2)).isoformat(),
    )
    client.post(f"{API}/expiry-register/scan", headers=admin_headers)
    entry = client.get(f"{API}/expiry-register", headers=admin_headers).json()["items"][0]

    without = client.post(
        f"{API}/expiry-register/{entry['id']}/return-to-supplier",
        json={"supplier_id": supplier["id"]}, headers=admin_headers,
    )
    assert without.status_code == 422

    body = client.post(
        f"{API}/expiry-register/{entry['id']}/return-to-supplier",
        json={"supplier_id": supplier["id"], "debit_note_ref": "DN-2026-14",
              "credit_expected": "500.00"},
        headers=admin_headers,
    ).json()
    assert body["status"] == "RETURNED_TO_SUPPLIER"
    assert body["debit_note_ref"] == "DN-2026-14"
    assert body["supplier_name"] == "Test Distributors"


def test_credit_can_only_be_recorded_against_a_return(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD",
        expiry_date=(business_date() - timedelta(days=2)).isoformat(),
    )
    client.post(f"{API}/expiry-register/scan", headers=admin_headers)
    entry = client.get(f"{API}/expiry-register", headers=admin_headers).json()["items"][0]

    too_early = client.post(
        f"{API}/expiry-register/{entry['id']}/record-credit",
        json={"amount": "100.00"}, headers=admin_headers,
    )
    assert too_early.status_code == 409
    assert too_early.json()["detail"]["code"] == "EXPIRY_NOT_RETURNED"


def test_an_entry_cannot_be_actioned_twice(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD",
        expiry_date=(business_date() - timedelta(days=2)).isoformat(),
    )
    client.post(f"{API}/expiry-register/scan", headers=admin_headers)
    entry = client.get(f"{API}/expiry-register", headers=admin_headers).json()["items"][0]

    client.post(f"{API}/expiry-register/{entry['id']}/write-off", json={}, headers=admin_headers)
    again = client.post(
        f"{API}/expiry-register/{entry['id']}/write-off", json={}, headers=admin_headers
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "EXPIRY_ALREADY_ACTIONED"


def test_the_expiry_register_is_readable_but_not_actionable(client, sales_headers):
    """The counter needs to know what has gone out of date.

    Acting on it — scanning, writing off, sending stock back — books a
    loss, so that stays with whoever holds `expiry:manage`.
    """
    assert client.get(f"{API}/expiry-register", headers=sales_headers).status_code == 200
    assert client.post(f"{API}/expiry-register/scan", headers=sales_headers).status_code == 403


# --- the order book -------------------------------------------------------


def test_selling_past_the_decline_threshold_raises_an_entry(client, admin_headers):
    """The roadmap's Phase 6 criterion."""
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="10")   # 100 units

    # 50 % gone — under the 60 % rule, nothing yet.
    sell(client, admin_headers, [line_of(medicine["id"], "50", discount_pct="0")])
    assert client.get(
        f"{API}/order-book", headers=admin_headers
    ).json()["total"] == 0

    # Past 60 %.
    sell(client, admin_headers, [line_of(medicine["id"], "15", discount_pct="0")])
    book = client.get(f"{API}/order-book", headers=admin_headers).json()
    assert book["total"] == 1

    entry = book["items"][0]
    assert entry["status"] == "OPEN"
    assert entry["trigger_reason"] == "STOCK_DECLINE_60"
    assert entry["baseline_units"] == "100.000"
    assert entry["available_units_at_trigger"] == "35.000"
    assert Decimal(entry["decline_pct"]) == Decimal("65.00")


def test_the_suggested_quantity_restores_the_baseline_in_whole_packs(
    client, admin_headers
):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="10")   # 100 units
    sell(client, admin_headers, [line_of(medicine["id"], "65", discount_pct="0")])

    entry = client.get(f"{API}/order-book", headers=admin_headers).json()["items"][0]
    # 35 left against a baseline of 100 → 65 short → 7 strips of ten.
    assert entry["suggested_qty_packs"] == "7.000"
    assert entry["suggested_qty_units"] == "70.000"


def test_running_out_is_its_own_trigger(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="1")
    sell(client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")])

    entry = client.get(f"{API}/order-book", headers=admin_headers).json()["items"][0]
    assert entry["trigger_reason"] == "OUT_OF_STOCK"
    assert entry["available_units_at_trigger"] == "0.000"


def test_one_medicine_never_has_two_open_entries(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="10")

    for _ in range(3):
        sell(client, admin_headers, [line_of(medicine["id"], "25", discount_pct="0")])

    book = client.get(f"{API}/order-book", headers=admin_headers).json()
    assert book["total"] == 1        # refreshed, not duplicated


def test_the_entry_carries_the_supplier_and_consumption(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"qty_packs": "10", "cost_per_pack": "80.00", "mrp_per_pack": "100.00"},
    )
    sell(client, admin_headers, [line_of(medicine["id"], "70", discount_pct="0")])

    entry = client.get(f"{API}/order-book", headers=admin_headers).json()["items"][0]
    assert entry["preferred_supplier_name"] == "Test Distributors"
    assert entry["last_purchase_cost"] == "80.00"
    assert Decimal(entry["avg_monthly_consumption_units"]) > 0
    assert entry["days_of_cover"] is not None
    assert entry["estimated_value"] is not None


def test_receiving_a_purchase_closes_the_entry_and_resets_the_baseline(
    client, admin_headers, db
):
    """The second half of the Phase 6 criterion."""
    from app.models.catalog import Medicine

    supplier = make_supplier(client, admin_headers)
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="10")
    sell(client, admin_headers, [line_of(medicine["id"], "70", discount_pct="0")])

    open_book = client.get(f"{API}/order-book", headers=admin_headers).json()
    assert open_book["total"] == 1

    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"qty_packs": "12", "batch_no": "REFILL"},
    )
    assert any("order-book" in w for w in purchase["warnings"])

    assert client.get(
        f"{API}/order-book", params={"status": "OPEN"}, headers=admin_headers
    ).json()["total"] == 0

    received = client.get(
        f"{API}/order-book", params={"status": "RECEIVED"}, headers=admin_headers
    ).json()
    assert received["total"] == 1
    assert received["items"][0]["purchase_invoice_id"] == purchase["id"]

    # The baseline now reflects the new delivery, so the next 60 % is
    # measured from here.
    db.expire_all()
    assert db.get(Medicine, medicine["id"]).stock_baseline_units == Decimal("220.000")


def test_recompute_sweeps_the_catalogue_and_closes_what_recovered(
    client, admin_headers
):
    medicine, batch = stocked_medicine(client, admin_headers, qty_packs="10")
    sell(client, admin_headers, [line_of(medicine["id"], "70", discount_pct="0")])
    assert client.get(f"{API}/order-book", headers=admin_headers).json()["total"] == 1

    # Stock comes back by hand rather than through a purchase.
    client.post(
        f"{API}/inventory/batches/{batch['id']}/adjust",
        json={"qty_units_delta": "80", "reason": "Found a carton in the back"},
        headers=admin_headers,
    )
    result = client.post(f"{API}/order-book/recompute", headers=admin_headers).json()
    assert result["closed"] == 1

    assert client.get(
        f"{API}/order-book", params={"status": "OPEN"}, headers=admin_headers
    ).json()["total"] == 0


def test_an_admin_can_adjust_and_mark_ordered(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="10")
    sell(client, admin_headers, [line_of(medicine["id"], "70", discount_pct="0")])
    entry = client.get(f"{API}/order-book", headers=admin_headers).json()["items"][0]

    body = client.patch(
        f"{API}/order-book/{entry['id']}",
        json={"suggested_qty_packs": "12", "preferred_supplier_id": supplier["id"],
              "status": "ORDERED", "notes": "Called the rep"},
        headers=admin_headers,
    ).json()

    assert body["suggested_qty_packs"] == "12.000"
    assert body["suggested_qty_units"] == "120.000"    # re-derived from the pack
    assert body["status"] == "ORDERED"
    assert body["ordered_at"] is not None
    assert body["preferred_supplier_name"] == "Test Distributors"


def test_the_order_book_exports_as_csv(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="10")
    sell(client, admin_headers, [line_of(medicine["id"], "70", discount_pct="0")])

    response = client.get(f"{API}/order-book/export", headers=admin_headers)
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    body = response.text
    assert "Supplier,Medicine,Pack" in body
    assert "Testolol 50" in body


def test_the_order_book_is_admin_only(client, sales_headers):
    assert client.get(f"{API}/order-book", headers=sales_headers).status_code == 403
    assert client.post(f"{API}/order-book/recompute", headers=sales_headers).status_code == 403
    assert client.get(f"{API}/order-book/export", headers=sales_headers).status_code == 403


def test_the_counter_can_still_read_the_alert_summary(client, sales_headers):
    body = client.get(f"{API}/alerts/summary", headers=sales_headers)
    assert body.status_code == 200
    assert "order_book_open" in body.json()
