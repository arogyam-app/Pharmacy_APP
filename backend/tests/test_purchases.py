"""Suppliers and goods inward.

The acceptance criteria for Phase 3: a purchase increases stock, the ledger
sum still equals `qty_available_units`, and a sales executive never sees a
cost figure anywhere.
"""

from app.db.types import uuid7
from app.utils.clock import business_date
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.conftest import API, make_medicine, make_purchase, make_supplier, stocked_medicine

TOMORROW = (business_date() + timedelta(days=400)).isoformat()


def _line(medicine_id, **kw):
    line = {
        "medicine_id": medicine_id,
        "batch_no": "PB-1",
        "expiry_date": TOMORROW,
        "qty_packs": "10",
        "cost_per_pack": "100.00",
        "mrp_per_pack": "150.00",
        "gst_rate": "12",
    }
    line.update(kw)
    return line


# --- suppliers -----------------------------------------------------------


def test_admin_creates_and_lists_suppliers(client, admin_headers):
    created = make_supplier(client, admin_headers, name="Meditrust", payment_terms_days=30)
    assert created["name"] == "Meditrust"
    assert created["purchase_count"] == 0

    listing = client.get(f"{API}/suppliers", headers=admin_headers).json()
    assert listing["total"] == 1
    assert listing["items"][0]["payment_terms_days"] == 30


def test_duplicate_supplier_name_is_rejected(client, admin_headers):
    make_supplier(client, admin_headers, name="Meditrust")
    response = client.post(
        f"{API}/suppliers", json={"name": "meditrust"}, headers=admin_headers
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SUPPLIER_EXISTS"


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/suppliers"),
        ("post", "/suppliers"),
        ("get", "/purchases"),
        ("post", "/purchases"),
    ],
)
def test_sales_exec_cannot_touch_the_buying_side(client, sales_headers, method, path):
    """Suppliers and purchases are the doorway to cost price."""
    response = client.request(method.upper(), f"{API}{path}", json={}, headers=sales_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_ROLE"


def test_supplier_with_purchases_is_deactivated_not_deleted(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    response = client.delete(f"{API}/suppliers/{supplier['id']}", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is False
    assert "deactivated" in body["message"]

    again = client.delete(f"{API}/suppliers/{supplier['id']}", headers=admin_headers)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "SUPPLIER_IN_USE"


def test_unused_supplier_is_deleted_outright(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    response = client.delete(f"{API}/suppliers/{supplier['id']}", headers=admin_headers)
    assert response.json()["deleted"] is True
    assert client.get(f"{API}/suppliers/{supplier['id']}", headers=admin_headers).status_code == 404


# --- goods inward --------------------------------------------------------


def test_purchase_creates_a_batch_and_increases_stock(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)  # 10 tablets per strip

    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])
    assert purchase["doc_number"] == "00000001"

    item = purchase["items"][0]
    assert item["qty_units"] == "100.000"       # 10 strips x 10 tablets
    assert item["batch_id"] is not None

    stock = client.get(
        f"{API}/inventory/stock", params={"q": "Testolol"}, headers=admin_headers
    ).json()
    assert stock["items"][0]["available_units"] == "100.000"


def test_free_goods_arrive_as_stock_but_are_not_billed(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)

    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"qty_packs": "10", "free_qty_packs": "2"},
    )
    item = purchase["items"][0]

    assert item["qty_units"] == "120.000"        # 12 strips landed
    assert item["taxable_value"] == "1000.00"    # only 10 were charged


def test_landed_cost_spreads_free_goods_over_the_whole_quantity(client, admin_headers, db):
    """10 + 2 free at ₹100 is a real cost of ₹83.33 a strip, not ₹100."""
    from app.models.inventory import Batch

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"qty_packs": "10", "free_qty_packs": "2"},
    )

    batch = db.get(Batch, purchase["items"][0]["batch_id"])
    assert batch.cost_per_pack == Decimal("83.33")
    # And the per-unit figure follows from it.
    assert batch.unit_cost == Decimal("8.3330")


def test_intra_state_purchase_splits_tax_into_cgst_and_sgst(client, admin_headers):
    supplier = make_supplier(client, admin_headers, state_code="18")  # same as the store
    medicine = make_medicine(client, admin_headers)

    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])
    assert purchase["is_intra_state"] is True
    assert purchase["subtotal_taxable"] == "1000.00"
    assert purchase["cgst_amount"] == "60.00"
    assert purchase["sgst_amount"] == "60.00"
    assert purchase["igst_amount"] == "0.00"
    assert purchase["total_amount"] == "1120.00"


def test_inter_state_purchase_uses_igst(client, admin_headers):
    supplier = make_supplier(client, admin_headers, state_code="07")  # store is 18
    medicine = make_medicine(client, admin_headers)

    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])
    assert purchase["is_intra_state"] is False
    assert purchase["igst_amount"] == "120.00"
    assert purchase["cgst_amount"] == "0.00"
    assert purchase["sgst_amount"] == "0.00"


def test_trade_discount_reduces_the_taxable_value(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)

    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"discount_pct": "10"},
    )
    item = purchase["items"][0]
    assert item["discount_amount"] == "100.00"
    assert item["taxable_value"] == "900.00"     # 1000 - 100
    assert item["line_total"] == "1008.00"       # 900 + 12 %


def test_odd_paise_still_reconcile_between_cgst_and_sgst(client, admin_headers):
    """Half of an odd number of paise must not vanish."""
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)

    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"qty_packs": "1", "cost_per_pack": "100.25", "gst_rate": "5"},
    )
    item = purchase["items"][0]
    total_tax = Decimal(item["cgst_amount"]) + Decimal(item["sgst_amount"])
    assert total_tax == Decimal("5.01")          # 100.25 x 5 % = 5.0125 -> 5.01
    assert Decimal(item["line_total"]) == Decimal("105.26")


def test_purchase_totals_add_up_across_lines(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    first = make_medicine(client, admin_headers)
    second = make_medicine(client, admin_headers, brand_name="Testolol 25")

    response = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "SUP-9",
            "supplier_invoice_date": business_date().isoformat(),
            "items": [
                _line(first["id"], batch_no="A1"),
                _line(second["id"], batch_no="A2", qty_packs="5", gst_rate="5"),
            ],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    lines = body["items"]
    assert sum(Decimal(i["taxable_value"]) for i in lines) == Decimal(body["subtotal_taxable"])
    tax = Decimal(body["cgst_amount"]) + Decimal(body["sgst_amount"]) + Decimal(body["igst_amount"])
    assert Decimal(body["total_amount"]) == Decimal(body["subtotal_taxable"]) + tax + Decimal(
        body["round_off"]
    )


def test_the_same_supplier_invoice_cannot_be_received_twice(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    response = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "SUP-001",
            "supplier_invoice_date": business_date().isoformat(),
            "items": [_line(medicine["id"])],
        },
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PURCHASE_EXISTS"


def test_repeat_consignment_of_one_lot_tops_up_the_same_batch(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)

    first = make_purchase(client, admin_headers, supplier["id"], medicine["id"])
    second = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "SUP-002",
            "supplier_invoice_date": business_date().isoformat(),
            "items": [_line(medicine["id"])],   # same batch_no and expiry
        },
        headers=admin_headers,
    ).json()

    assert second["items"][0]["batch_id"] == first["items"][0]["batch_id"]

    batches = client.get(
        f"{API}/inventory/batches",
        params={"medicine_id": medicine["id"]},
        headers=admin_headers,
    ).json()
    assert batches["total"] == 1
    assert batches["items"][0]["qty_available_units"] == "200.000"


def test_a_differing_mrp_on_a_repeat_batch_is_flagged_not_applied(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    second = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "SUP-003",
            "supplier_invoice_date": business_date().isoformat(),
            "items": [_line(medicine["id"], mrp_per_pack="175.00")],
        },
        headers=admin_headers,
    ).json()

    assert any("differs" in w for w in second["warnings"])
    batches = client.get(
        f"{API}/inventory/batches",
        params={"medicine_id": medicine["id"]},
        headers=admin_headers,
    ).json()
    assert batches["items"][0]["mrp_per_pack"] == "150.00"   # unchanged


def test_receiving_an_expired_lot_warns(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)

    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"expiry_date": (business_date() - timedelta(days=1)).isoformat()},
    )
    assert any("cannot be sold" in w for w in purchase["warnings"])


def test_cost_above_mrp_is_rejected(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)

    response = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "SUP-004",
            "supplier_invoice_date": business_date().isoformat(),
            "items": [_line(medicine["id"], cost_per_pack="200.00", mrp_per_pack="150.00")],
        },
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_purchase_document_numbers_are_sequential_and_padded(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)

    numbers = []
    for index in range(3):
        body = client.post(
            f"{API}/purchases",
            json={
                "supplier_id": supplier["id"],
                "supplier_invoice_no": f"SEQ-{index}",
                "supplier_invoice_date": business_date().isoformat(),
                "items": [_line(medicine["id"], batch_no=f"SEQ{index}")],
            },
            headers=admin_headers,
        ).json()
        numbers.append(body["doc_number"])

    assert numbers == ["00000001", "00000002", "00000003"]


# --- the ledger invariant ------------------------------------------------


def test_ledger_sum_still_equals_available_after_a_purchase(client, admin_headers, db):
    """CLAUDE.md rule 3, checked across the whole goods-inward path."""
    from sqlalchemy import func

    from app.models.inventory import Batch, StockLedger

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])
    make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        supplier_invoice_no="SUP-B",
        line={"batch_no": "PB-2"},
    )

    for batch in db.query(Batch).all():
        ledger_sum = (
            db.query(func.sum(StockLedger.qty_units_delta))
            .filter(StockLedger.batch_id == batch.id)
            .scalar()
        )
        assert batch.qty_available_units == ledger_sum


def test_purchase_writes_a_ledger_row_referencing_the_line(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    ledger = client.get(
        f"{API}/inventory/ledger",
        params={"medicine_id": medicine["id"]},
        headers=admin_headers,
    ).json()
    entry = ledger["items"][0]
    assert entry["movement_type"] == "PURCHASE"
    assert entry["reference_type"] == "PURCHASE_ITEM"
    assert entry["reference_id"] == purchase["items"][0]["id"]


# --- cancellation --------------------------------------------------------


def test_cancelling_a_purchase_reverses_the_stock(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    response = client.post(
        f"{API}/purchases/{purchase['id']}/cancel",
        json={"reason": "Keyed against the wrong supplier"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["is_cancelled"] is True

    stock = client.get(
        f"{API}/inventory/stock", params={"q": "Testolol"}, headers=admin_headers
    ).json()
    assert stock["items"][0]["available_units"] == "0.000"


def test_cancelling_leaves_the_document_and_its_figures_intact(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    client.post(
        f"{API}/purchases/{purchase['id']}/cancel",
        json={"reason": "Duplicate entry"},
        headers=admin_headers,
    )
    after = client.get(f"{API}/purchases/{purchase['id']}", headers=admin_headers).json()

    assert after["total_amount"] == purchase["total_amount"]
    assert len(after["items"]) == 1
    assert after["cancel_reason"] == "Duplicate entry"
    assert after["cancelled_at"] is not None


def test_cancelling_twice_is_refused(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    body = {"reason": "Wrong supplier"}
    client.post(f"{API}/purchases/{purchase['id']}/cancel", json=body, headers=admin_headers)
    again = client.post(
        f"{API}/purchases/{purchase['id']}/cancel", json=body, headers=admin_headers
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "PURCHASE_CANCELLED"


def test_cancelling_is_refused_once_stock_has_been_sold(client, admin_headers, db):
    """Simulates the Phase 4 sale that will point at this batch."""
    from app.models.enums import MovementType, ReferenceType
    from app.models.inventory import Batch
    from app.services import inventory as inventory_service

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    batch = db.get(Batch, purchase["items"][0]["batch_id"])
    inventory_service.post_movement(
        db,
        batch=batch,
        movement_type=MovementType.SALE,
        qty_units_delta=Decimal("-1"),
        reference_type=ReferenceType.SALE_ITEM,
        reference_id=uuid7(),
    )
    db.commit()

    response = client.post(
        f"{API}/purchases/{purchase['id']}/cancel",
        json={"reason": "Too late"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PURCHASE_HAS_SALES"


def test_cancelling_is_refused_when_the_shelf_is_already_short(client, admin_headers):
    """An adjustment took stock out; cancelling would drive it negative."""
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    client.post(
        f"{API}/inventory/batches/{purchase['items'][0]['batch_id']}/adjust",
        json={"qty_units_delta": "-5", "reason": "Breakage"},
        headers=admin_headers,
    )
    response = client.post(
        f"{API}/purchases/{purchase['id']}/cancel",
        json={"reason": "Entered in error"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PURCHASE_STOCK_SHORT"


# --- register and cost gating -------------------------------------------


def test_register_filters_by_supplier_and_search(client, admin_headers):
    first = make_supplier(client, admin_headers, name="Meditrust")
    second = make_supplier(client, admin_headers, name="Northline")
    medicine = make_medicine(client, admin_headers)

    make_purchase(client, admin_headers, first["id"], medicine["id"])
    make_purchase(
        client, admin_headers, second["id"], medicine["id"],
        supplier_invoice_no="NL-77", line={"batch_no": "NL-1"},
    )

    by_supplier = client.get(
        f"{API}/purchases", params={"supplier_id": second["id"]}, headers=admin_headers
    ).json()
    assert by_supplier["total"] == 1
    assert by_supplier["items"][0]["supplier_name"] == "Northline"

    by_number = client.get(
        f"{API}/purchases", params={"q": "NL-77"}, headers=admin_headers
    ).json()
    assert by_number["total"] == 1


def test_cost_from_a_purchase_never_reaches_a_sales_exec(client, admin_headers, sales_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    batches = client.get(
        f"{API}/inventory/batches",
        params={"medicine_id": medicine["id"]},
        headers=sales_headers,
    ).json()
    row = batches["items"][0]
    assert "cost_per_pack" not in row
    assert "unit_cost" not in row
    assert "margin_pct" not in row
    assert row["mrp_per_pack"] == "150.00"     # price is theirs to see


def test_payment_status_is_editable_but_the_figures_are_not(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])

    response = client.patch(
        f"{API}/purchases/{purchase['id']}",
        json={"payment_status": "PAID", "total_amount": "1.00"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["payment_status"] == "PAID"
    assert body["total_amount"] == purchase["total_amount"]


def test_every_purchase_action_is_audited(client, admin_headers, db):
    from app.models.audit import AuditLog

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])
    client.post(
        f"{API}/purchases/{purchase['id']}/cancel",
        json={"reason": "Wrong supplier"},
        headers=admin_headers,
    )

    actions = {
        (row.action, row.entity_type)
        for row in db.query(AuditLog).filter(AuditLog.entity_type.in_(
            ["Supplier", "PurchaseInvoice"]
        ))
    }
    assert ("CREATE", "Supplier") in actions
    assert ("CREATE", "PurchaseInvoice") in actions
    assert ("VOID", "PurchaseInvoice") in actions


def test_purchase_price_reaches_the_counter_through_fefo(client, admin_headers):
    """The batch a purchase creates is what the counter quotes."""
    supplier = make_supplier(client, admin_headers)
    medicine, _ = stocked_medicine(client, admin_headers, mrp="100.00")

    # A newer consignment with a later expiry must not displace the old one.
    make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={"batch_no": "NEW-1", "mrp_per_pack": "180.00"},
    )
    results = client.get(
        f"{API}/medicines/search", params={"q": "Testolol"}, headers=admin_headers
    ).json()
    assert results[0]["mrp_per_pack"] == "100.00"   # earliest expiry wins
    assert results[0]["available_units"] == "200.000"   # both batches count
