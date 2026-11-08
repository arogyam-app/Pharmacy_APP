"""Inventory: batches carry the money, the ledger is the source of truth.

MRP, GST rate and discount limits moved here from the medicine master
because they change with each consignment.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func

from app.core.permissions import default_permissions
from app.models.enums import UserRole
from app.models.inventory import Batch, StockLedger
from app.utils.clock import business_date
from tests.conftest import API, make_batch, make_medicine

TODAY = business_date()


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# --- the ledger invariant ------------------------------------------------


def test_receiving_stock_writes_a_ledger_row(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="5")

    assert Decimal(batch["qty_available_units"]) == Decimal("50.000")  # 5 packs x 10

    entry = db.query(StockLedger).filter(StockLedger.batch_id == batch["id"]).one()
    assert entry.movement_type == "OPENING"
    assert entry.qty_units_delta == Decimal("50.000")
    assert entry.balance_after_units == Decimal("50.000")


def test_batch_quantity_always_equals_its_ledger_sum(client, admin_headers, db):
    """CLAUDE.md rule 3: qty_available_units is a cached projection."""
    for i in range(3):
        medicine = make_medicine(client, admin_headers, brand_name=f"Ledger {i}", pack_size="10")
        make_batch(client, admin_headers, medicine["id"], qty_packs=str(i + 1))

    for batch in db.query(Batch).all():
        ledger_sum = (
            db.query(func.sum(StockLedger.qty_units_delta))
            .filter(StockLedger.batch_id == batch.id)
            .scalar()
        )
        assert batch.qty_available_units == ledger_sum


def test_a_syrup_batch_counts_bottles_not_millilitres(client, admin_headers):
    medicine = make_medicine(
        client, admin_headers, brand_name="Syrupy", dosage_form="SYRUP", pack_size="100"
    )
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="6", mrp_per_pack="128.00")

    assert Decimal(batch["qty_available_units"]) == Decimal("6.000")  # six bottles
    assert Decimal(batch["unit_mrp"]) == Decimal("128.0000")  # per bottle


# --- price and discount resolution ---------------------------------------


def test_price_comes_from_the_batch_not_the_master(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="15")

    before = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    assert before["mrp_per_pack"] is None  # no stock, no price

    make_batch(client, admin_headers, medicine["id"], mrp_per_pack="30.00")

    after = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    assert Decimal(after["mrp_per_pack"]) == Decimal("30.00")
    assert Decimal(after["unit_mrp"]) == Decimal("2.0000")
    assert Decimal(after["gst_rate"]) == Decimal("12.00")


def test_a_new_consignment_can_change_the_price_and_the_tax(client, admin_headers):
    """The reason these fields moved: MRP and GST slabs change over time."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(
        client, admin_headers, medicine["id"], batch_no="OLD",
        expiry_date=iso(120), mrp_per_pack="50.00", gst_rate="12",
    )
    make_batch(
        client, admin_headers, medicine["id"], batch_no="NEW",
        expiry_date=iso(400), mrp_per_pack="62.00", gst_rate="5",
    )

    fetched = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    # FEFO: the older-expiry lot sells first, so its price is the live one.
    assert Decimal(fetched["mrp_per_pack"]) == Decimal("50.00")
    assert Decimal(fetched["gst_rate"]) == Decimal("12.00")
    assert fetched["batch_count"] == 2
    assert Decimal(fetched["available_units"]) == Decimal("200.000")


def test_expired_stock_is_never_priced_or_counted(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(
        client, admin_headers, medicine["id"], batch_no="GONE",
        expiry_date=iso(-1), mrp_per_pack="50.00",
    )

    fetched = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    assert fetched["mrp_per_pack"] is None
    assert Decimal(fetched["available_units"]) == Decimal("0.000")


def test_blocked_stock_is_excluded(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], mrp_per_pack="50.00")

    client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"is_blocked": True, "block_reason": "Quarantined"},
        headers=admin_headers,
    )

    fetched = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    assert fetched["mrp_per_pack"] is None
    assert Decimal(fetched["available_units"]) == Decimal("0.000")


def test_near_expiry_stock_is_flagged(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(client, admin_headers, medicine["id"], expiry_date=iso(30))

    fetched = client.get(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()
    assert fetched["has_near_expiry"] is True


def test_batch_discount_limits_fall_back_to_store_policy(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    batch = make_batch(client, admin_headers, medicine["id"])

    assert batch["default_discount_pct"] is None
    assert Decimal(batch["effective_default_discount_pct"]) == Decimal("10.00")
    assert Decimal(batch["effective_max_discount_pct"]) == Decimal("20.00")


def test_default_above_max_on_a_batch_is_rejected(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    response = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": medicine["id"], "batch_no": "X1", "expiry_date": iso(365),
            "qty_packs": "5", "mrp_per_pack": "50.00",
            "default_discount_pct": "30", "max_discount_pct": "10",
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_unit_prices_follow_a_corrected_pack_price(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], mrp_per_pack="50.00")

    updated = client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"mrp_per_pack": "60.00"},
        headers=admin_headers,
    ).json()

    assert Decimal(updated["unit_mrp"]) == Decimal("6.0000")


# --- cost price is admin-only -------------------------------------------


def test_admin_sees_cost_and_margin(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(
        client, admin_headers, medicine["id"], mrp_per_pack="100.00", cost_per_pack="80.00"
    )

    assert Decimal(batch["cost_per_pack"]) == Decimal("80.00")
    assert Decimal(batch["unit_cost"]) == Decimal("8.0000")
    assert Decimal(batch["margin_pct"]) == Decimal("25.00")


def test_sales_executive_never_receives_cost_price(client, admin_headers, sales_headers):
    """CLAUDE.md rule 4 — enforced by returning a different response model,
    not by blanking a field."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(client, admin_headers, medicine["id"], mrp_per_pack="100.00", cost_per_pack="80.00")

    response = client.get(
        f"{API}/inventory/batches?medicine_id={medicine['id']}", headers=sales_headers
    )
    assert response.status_code == 200
    row = response.json()["items"][0]

    assert "cost_per_pack" not in row
    assert "unit_cost" not in row
    assert "margin_pct" not in row
    # The literal decimal, not a bare "80" -- that substring can appear by
    # chance inside a random UUID's hex and false-fail the test.
    assert "80.00" not in response.text
    # Selling price is still visible — the counter needs it.
    assert Decimal(row["mrp_per_pack"]) == Decimal("100.00")


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/inventory/batches"), ("get", "/inventory/ledger")],
)
def test_sales_executive_cannot_manage_stock(client, sales_headers, method, path):
    response = client.request(method.upper(), f"{API}{path}", json={}, headers=sales_headers)
    assert response.status_code == 403


def test_sales_executive_can_read_stock_on_hand(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(client, admin_headers, medicine["id"], cost_per_pack="80.00")

    response = client.get(f"{API}/inventory/stock", headers=sales_headers)
    assert response.status_code == 200
    # The literal decimal, not a bare "80" -- that substring can appear by
    # chance inside a random UUID's hex and false-fail the test.
    assert "80.00" not in response.text


# --- integrity -----------------------------------------------------------


def test_duplicate_batch_number_and_expiry_is_rejected(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"], batch_no="DUP", expiry_date=iso(365))

    response = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": medicine["id"], "batch_no": "DUP", "expiry_date": iso(365),
            "qty_packs": "5", "mrp_per_pack": "50.00",
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BATCH_EXISTS"


def test_the_same_batch_number_at_a_different_expiry_is_allowed(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"], batch_no="B1", expiry_date=iso(200))
    second = make_batch(client, admin_headers, medicine["id"], batch_no="B1", expiry_date=iso(400))

    assert second["id"]


def test_batch_snapshots_the_pack_so_master_edits_do_not_rewrite_history(
    client, admin_headers, db
):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="5")

    client.patch(
        f"{API}/medicines/{medicine['id']}", json={"pack_size": "15"}, headers=admin_headers
    )

    stored = db.get(Batch, batch["id"])
    assert stored.units_per_pack == 10  # what actually arrived
    assert stored.qty_available_units == Decimal("50.000")


def test_stock_on_hand_reports_availability(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(client, admin_headers, medicine["id"], qty_packs="4")

    rows = client.get(f"{API}/inventory/stock", headers=admin_headers).json()["items"]
    row = next(r for r in rows if r["medicine_id"] == medicine["id"])

    assert Decimal(row["available_units"]) == Decimal("40.000")
    assert Decimal(row["available_packs"]) == Decimal("4.000")
    assert row["batch_count"] == 1


def test_low_stock_is_flagged_against_the_store_threshold(client, admin_headers):
    medicine = make_medicine(client, admin_headers, brand_name="Scarce", pack_size="1")
    make_batch(client, admin_headers, medicine["id"], qty_packs="3")  # below the default 10

    rows = client.get(f"{API}/inventory/stock?low_stock_only=true", headers=admin_headers).json()
    assert medicine["id"] in {r["medicine_id"] for r in rows["items"]}


# --- the Update stock panel ----------------------------------------------
#
# It offers only medicines that have been purchased before, because a brand's
# first consignment belongs on a supplier invoice.


def test_stocked_only_lists_medicines_that_have_a_batch(client, admin_headers):
    bought = make_medicine(client, admin_headers, brand_name="Bought Once")
    never = make_medicine(client, admin_headers, brand_name="Never Bought")
    make_batch(client, admin_headers, bought["id"])

    rows = client.get(f"{API}/inventory/stock?stocked_only=true", headers=admin_headers).json()
    listed = {r["medicine_id"] for r in rows["items"]}

    assert bought["id"] in listed
    assert never["id"] not in listed


def test_stocked_only_keeps_a_medicine_whose_stock_has_run_out(client, admin_headers):
    """Sold out is not the same as never bought — you still top that batch up."""
    medicine = make_medicine(client, admin_headers, pack_size="1")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="2")
    client.post(
        f"{API}/inventory/batches/{batch['id']}/adjust",
        json={"qty_units_delta": "-2", "reason": "Sold out in a test"},
        headers=admin_headers,
    )

    rows = client.get(
        f"{API}/inventory/stock?stocked_only=true&q=", headers=admin_headers
    ).json()
    row = next(r for r in rows["items"] if r["medicine_id"] == medicine["id"])

    assert Decimal(row["available_units"]) == Decimal("0.000")


def test_stock_rows_carry_the_pack_unit(client, admin_headers):
    """The panel needs it to say "12 tablet(s)" rather than "12 unit(s)"."""
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"])

    rows = client.get(f"{API}/inventory/stock", headers=admin_headers).json()["items"]
    row = next(r for r in rows if r["medicine_id"] == medicine["id"])

    assert row["pack_unit"] == "TABLET"


def _grant(client, admin_headers, user_id, *extra):
    """Give a sales executive their defaults plus `extra`."""
    wanted = set(default_permissions(UserRole.SALES_EXEC.value)) | set(extra)
    response = client.put(
        f"{API}/users/{user_id}/permissions",
        json={"permissions": sorted(wanted), "note": None},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text


def test_receive_goods_carries_the_right_to_update_a_batch(
    client, admin_headers, sales_headers, sales
):
    """Receiving goods *is* writing a batch, so the panel is not a dead door."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="4")
    _grant(client, admin_headers, sales.id, "purchases:receive")

    terms = client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"mrp_per_pack": "120.00"},
        headers=sales_headers,
    )
    assert terms.status_code == 200, terms.text

    moved = client.post(
        f"{API}/inventory/batches/{batch['id']}/adjust",
        json={"qty_units_delta": "10", "reason": "Consignment received at the counter"},
        headers=sales_headers,
    )
    assert moved.status_code == 200, moved.text
    assert Decimal(moved.json()["qty_available_units"]) == Decimal("50.000")

    fresh = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": medicine["id"], "batch_no": "NEW-1", "expiry_date": iso(400),
            "qty_packs": "3", "mrp_per_pack": "120.00",
        },
        headers=sales_headers,
    )
    assert fresh.status_code == 201, fresh.text


def test_a_receiver_updating_a_batch_still_never_sees_cost(
    client, admin_headers, sales_headers, sales
):
    """CLAUDE.md rule 4 does not bend for a delegated capability."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(
        client, admin_headers, medicine["id"], mrp_per_pack="100.00", cost_per_pack="80.00"
    )
    _grant(client, admin_headers, sales.id, "purchases:receive")

    response = client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"mrp_per_pack": "110.00"},
        headers=sales_headers,
    )
    assert response.status_code == 200, response.text
    assert "cost_per_pack" not in response.json()
    assert "margin_pct" not in response.json()
    # The literal decimal, not a bare "80" -- that substring can appear by
    # chance inside a random UUID's hex and false-fail the test.
    assert "80.00" not in response.text


def test_updating_a_batch_leaves_a_cost_the_receiver_cannot_see_alone(
    client, admin_headers, sales_headers, sales, db
):
    """The PATCH omits what it cannot show, so cost survives the round trip."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(
        client, admin_headers, medicine["id"], mrp_per_pack="100.00", cost_per_pack="80.00"
    )
    _grant(client, admin_headers, sales.id, "purchases:receive")

    client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"mrp_per_pack": "110.00"},
        headers=sales_headers,
    )

    stored = db.get(Batch, batch["id"])
    assert stored.cost_per_pack == Decimal("80.00")
    assert stored.mrp_per_pack == Decimal("110.00")


def test_without_receive_goods_a_sales_executive_still_cannot_write_a_batch(
    client, admin_headers, sales_headers
):
    medicine = make_medicine(client, admin_headers)
    batch = make_batch(client, admin_headers, medicine["id"])

    response = client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"mrp_per_pack": "120.00"},
        headers=sales_headers,
    )
    assert response.status_code == 403
