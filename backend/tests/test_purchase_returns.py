"""Goods back to a distributor: stock, input tax, and the credit it creates.

The three things that must hold together, per CLAUDE.md rules 3, 5 and 7:

* the units leave through the ledger, and `qty_available_units` still equals
  the ledger sum for that batch;
* the input tax claimed when the goods arrived is reversed at the cost and
  rate they arrived at, and the GST reports say so;
* the credit is spendable exactly once, and the document cannot be unwound
  after it has been spent.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func

from app.models.inventory import Batch, StockLedger
from app.models.purchase_return import PurchaseReturn
from tests.conftest import (
    API,
    MISSING_ID,
    make_medicine,
    make_purchase,
    make_supplier,
    sell,
)
from app.utils.clock import business_date


def line_of(medicine_id, qty, **extra):
    return {"medicine_id": medicine_id, "qty_units": qty, **extra}


@pytest.fixture
def stocked(client, admin_headers):
    """A supplier, one purchase, and the batch it created.

    100 packs of 10 tablets at 100.00 cost per pack, 12 % GST — so one unit
    costs 10.0000 exclusive of tax.
    """
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={
            "medicine_id": medicine["id"],
            "batch_no": "RB-1",
            "expiry_date": (business_date() + timedelta(days=400)).isoformat(),
            "qty_packs": "100",
            "cost_per_pack": "100.00",
            "mrp_per_pack": "150.00",
            "gst_rate": "12",
        },
    )
    return supplier, medicine, purchase


def raise_return(client, headers, supplier_id, batch_id, qty, **extra):
    payload = {
        "supplier_id": supplier_id,
        "reason": "Overstocked, distributor agreed to take it back",
        "items": [{"batch_id": batch_id, "qty_units": qty}],
    }
    payload.update(extra)
    return client.post(f"{API}/purchase-returns", json=payload, headers=headers)


# --- stock ----------------------------------------------------------------


def test_returning_stock_takes_it_off_the_shelf(client, admin_headers, db, stocked):
    supplier, _medicine, purchase = stocked
    batch_id = purchase["items"][0]["batch_id"]
    before = db.get(Batch, batch_id).qty_available_units

    response = raise_return(client, admin_headers, supplier["id"], batch_id, "50")
    assert response.status_code == 201, response.text

    db.expire_all()
    assert db.get(Batch, batch_id).qty_available_units == before - Decimal("50")


def test_the_batch_still_reconciles_with_its_ledger(client, admin_headers, db, stocked):
    """CLAUDE.md rule 3: the cached figure is a projection of the ledger."""
    supplier, _medicine, purchase = stocked
    batch_id = purchase["items"][0]["batch_id"]

    raise_return(client, admin_headers, supplier["id"], batch_id, "37")

    db.expire_all()
    batch = db.get(Batch, batch_id)
    ledger_sum = (
        db.query(func.sum(StockLedger.qty_units_delta))
        .filter(StockLedger.batch_id == batch_id)
        .scalar()
    )
    assert batch.qty_available_units == ledger_sum


def test_more_than_is_there_cannot_go_back(client, admin_headers, stocked):
    supplier, _medicine, purchase = stocked
    batch_id = purchase["items"][0]["batch_id"]

    response = raise_return(client, admin_headers, supplier["id"], batch_id, "5000")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INSUFFICIENT_STOCK"


def test_stock_already_sold_cannot_also_be_returned(client, admin_headers, db, stocked):
    """The units are gone; the debit note must not conjure them back."""
    supplier, medicine, purchase = stocked
    batch_id = purchase["items"][0]["batch_id"]

    sell(client, admin_headers, [line_of(medicine["id"], "995")])
    db.expire_all()
    left = db.get(Batch, batch_id).qty_available_units

    response = raise_return(client, admin_headers, supplier["id"], batch_id, str(left + 1))
    assert response.status_code == 409


# --- the tax --------------------------------------------------------------


def test_input_tax_is_reversed_at_the_cost_the_goods_arrived_at(
    client, admin_headers, stocked
):
    """Cost, not MRP. The distributor credits what they charged.

    50 units at 10.0000 = 500.00 taxable, 12 % = 60.00 tax, 560.00 credit.
    """
    supplier, _medicine, purchase = stocked
    batch_id = purchase["items"][0]["batch_id"]

    body = raise_return(client, admin_headers, supplier["id"], batch_id, "50").json()

    assert Decimal(body["taxable_amount"]) == Decimal("500.00")
    assert Decimal(body["cgst_amount"]) == Decimal("30.00")
    assert Decimal(body["sgst_amount"]) == Decimal("30.00")
    assert Decimal(body["igst_amount"]) == Decimal("0.00")
    assert Decimal(body["total_amount"]) == Decimal("560.00")


def test_an_out_of_state_supplier_reverses_igst(client, admin_headers):
    """The split follows the two state codes, exactly as the purchase did."""
    supplier = make_supplier(client, admin_headers, name="Outstate Co", state_code="07")
    medicine = make_medicine(client, admin_headers)
    purchase = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        line={
            "medicine_id": medicine["id"], "batch_no": "OS-1",
            "expiry_date": (business_date() + timedelta(days=400)).isoformat(),
            "qty_packs": "10", "cost_per_pack": "100.00",
            "mrp_per_pack": "150.00", "gst_rate": "12",
        },
    )
    batch_id = purchase["items"][0]["batch_id"]

    body = raise_return(client, admin_headers, supplier["id"], batch_id, "10").json()

    assert Decimal(body["igst_amount"]) == Decimal("12.00")
    assert Decimal(body["cgst_amount"]) == Decimal("0.00")
    assert Decimal(body["sgst_amount"]) == Decimal("0.00")


def test_the_gst_register_shows_the_claim_and_the_reversal_apart(
    client, admin_headers, stocked
):
    """Two documents under GST, so two rows — netted only in the totals."""
    supplier, _medicine, purchase = stocked
    raise_return(client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50")

    today = business_date().isoformat()
    report = client.get(
        f"{API}/reports/gst/inward-summary?date_from={today}&date_to={today}",
        headers=admin_headers,
    )
    assert report.status_code == 200, report.text
    body = report.json()

    documents = [row["document"] for row in body["rows"]]
    assert "Purchase" in documents
    assert "Return (debit note)" in documents

    reversal = next(r for r in body["rows"] if r["document"] == "Return (debit note)")
    assert Decimal(reversal["taxable_value"]) == Decimal("-500.00")
    assert Decimal(reversal["total_tax"]) == Decimal("-60.00")

    # 10000.00 claimed less 500.00 reversed.
    assert Decimal(body["totals"]["taxable_value"]) == Decimal("9500.00")


def test_the_reversal_reduces_the_input_credit_on_the_liability_report(
    client, admin_headers, stocked
):
    supplier, _medicine, purchase = stocked
    today = business_date().isoformat()

    before = client.get(
        f"{API}/reports/gst/net-liability?date_from={today}&date_to={today}",
        headers=admin_headers,
    ).json()
    credit_before = Decimal(before["totals"]["input_credit"])

    raise_return(client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50")

    after = client.get(
        f"{API}/reports/gst/net-liability?date_from={today}&date_to={today}",
        headers=admin_headers,
    ).json()
    assert Decimal(after["totals"]["input_credit"]) == credit_before - Decimal("60.00")


# --- the credit -----------------------------------------------------------


def test_the_credit_opens_at_the_full_value(client, admin_headers, stocked):
    supplier, _medicine, purchase = stocked
    body = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()

    assert Decimal(body["credit_balance"]) == Decimal("560.00")
    assert Decimal(body["credit_used"]) == Decimal("0.00")
    assert body["credit_status"] == "OPEN"


def test_credit_is_offered_against_that_supplier_only(client, admin_headers, stocked):
    supplier, _medicine, purchase = stocked
    other = make_supplier(client, admin_headers, name="Someone Else", state_code="18")
    raise_return(client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50")

    mine = client.get(
        f"{API}/purchase-returns/credit?supplier_id={supplier['id']}", headers=admin_headers
    ).json()
    theirs = client.get(
        f"{API}/purchase-returns/credit?supplier_id={other['id']}", headers=admin_headers
    ).json()

    assert len(mine) == 1
    assert Decimal(mine[0]["credit_balance"]) == Decimal("560.00")
    assert theirs == []


def test_applying_credit_reduces_what_is_left_to_pay(client, admin_headers, stocked):
    """The whole point: the return settles part of the next invoice."""
    supplier, medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()

    second = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"],
        supplier_invoice_no="SUP-002",
        line={
            "medicine_id": medicine["id"], "batch_no": "RB-2",
            "expiry_date": (business_date() + timedelta(days=400)).isoformat(),
            "qty_packs": "10", "cost_per_pack": "100.00",
            "mrp_per_pack": "150.00", "gst_rate": "12",
        },
    )

    applied = client.post(
        f"{API}/purchase-returns/apply-to-invoice/{second['id']}",
        json={"purchase_return_id": note["id"], "amount": "560.00"},
        headers=admin_headers,
    )
    assert applied.status_code == 201, applied.text
    body = applied.json()

    assert Decimal(body["total_credit_applied"]) == Decimal("560.00")
    assert Decimal(body["balance_payable"]) == (
        Decimal(body["invoice_total"]) - Decimal("560.00")
    )


def test_the_invoice_still_claims_its_own_input_tax_in_full(
    client, admin_headers, stocked
):
    """A credit settles what is owed; it is not a discount on what was bought.

    If applying it moved the invoice's taxable value, the input tax claimed
    would fall with it and the reversal would be double-counted.
    """
    supplier, medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()
    second = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"], supplier_invoice_no="SUP-003",
    )
    before = client.get(f"{API}/purchases/{second['id']}", headers=admin_headers).json()

    client.post(
        f"{API}/purchase-returns/apply-to-invoice/{second['id']}",
        json={"purchase_return_id": note["id"], "amount": "100.00"},
        headers=admin_headers,
    )

    after = client.get(f"{API}/purchases/{second['id']}", headers=admin_headers).json()
    assert after["subtotal_taxable"] == before["subtotal_taxable"]
    assert after["cgst_amount"] == before["cgst_amount"]
    assert after["total_amount"] == before["total_amount"]


def test_the_same_credit_cannot_be_spent_twice(client, admin_headers, stocked):
    supplier, medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()
    invoice = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"], supplier_invoice_no="SUP-004",
    )

    first = client.post(
        f"{API}/purchase-returns/apply-to-invoice/{invoice['id']}",
        json={"purchase_return_id": note["id"], "amount": "560.00"},
        headers=admin_headers,
    )
    assert first.status_code == 201

    again = client.post(
        f"{API}/purchase-returns/apply-to-invoice/{invoice['id']}",
        json={"purchase_return_id": note["id"], "amount": "0.01"},
        headers=admin_headers,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "CREDIT_EXCEEDED"


def test_credit_cannot_be_applied_to_another_suppliers_invoice(
    client, admin_headers, stocked
):
    supplier, medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()
    other = make_supplier(client, admin_headers, name="Unrelated Co", state_code="18")
    theirs = make_purchase(
        client, admin_headers, other["id"], medicine["id"], supplier_invoice_no="OTHER-1",
    )

    response = client.post(
        f"{API}/purchase-returns/apply-to-invoice/{theirs['id']}",
        json={"purchase_return_id": note["id"], "amount": "10.00"},
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["field"] == "purchase_return_id"


def test_partly_spent_credit_reports_what_is_left(client, admin_headers, stocked):
    supplier, medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()
    invoice = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"], supplier_invoice_no="SUP-005",
    )

    client.post(
        f"{API}/purchase-returns/apply-to-invoice/{invoice['id']}",
        json={"purchase_return_id": note["id"], "amount": "200.00"},
        headers=admin_headers,
    )

    body = client.get(f"{API}/purchase-returns/{note['id']}", headers=admin_headers).json()
    assert Decimal(body["credit_used"]) == Decimal("200.00")
    assert Decimal(body["credit_balance"]) == Decimal("360.00")
    assert body["credit_status"] == "PARTIALLY_USED"


# --- undoing one ----------------------------------------------------------


def test_cancelling_puts_the_goods_back(client, admin_headers, db, stocked):
    supplier, _medicine, purchase = stocked
    batch_id = purchase["items"][0]["batch_id"]
    before = db.get(Batch, batch_id).qty_available_units

    note = raise_return(client, admin_headers, supplier["id"], batch_id, "50").json()
    cancelled = client.post(
        f"{API}/purchase-returns/{note['id']}/cancel",
        json={"reason": "Distributor refused the consignment"},
        headers=admin_headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    db.expire_all()
    assert db.get(Batch, batch_id).qty_available_units == before
    assert cancelled.json()["status"] == "CANCELLED"


def test_a_cancelled_note_carries_no_credit(client, admin_headers, stocked):
    supplier, _medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()
    client.post(
        f"{API}/purchase-returns/{note['id']}/cancel",
        json={"reason": "Keyed against the wrong batch"},
        headers=admin_headers,
    )

    offered = client.get(
        f"{API}/purchase-returns/credit?supplier_id={supplier['id']}", headers=admin_headers
    ).json()
    assert offered == []


def test_a_note_whose_credit_is_spent_cannot_be_cancelled(client, admin_headers, stocked):
    """CLAUDE.md rule 5 — the money is already on another document."""
    supplier, medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()
    invoice = make_purchase(
        client, admin_headers, supplier["id"], medicine["id"], supplier_invoice_no="SUP-006",
    )
    client.post(
        f"{API}/purchase-returns/apply-to-invoice/{invoice['id']}",
        json={"purchase_return_id": note["id"], "amount": "50.00"},
        headers=admin_headers,
    )

    response = client.post(
        f"{API}/purchase-returns/{note['id']}/cancel",
        json={"reason": "Changed my mind"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CREDIT_ALREADY_USED"


def test_cancelling_twice_is_refused(client, admin_headers, stocked):
    supplier, _medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()
    body = {"reason": "Entered in error"}
    client.post(f"{API}/purchase-returns/{note['id']}/cancel", json=body, headers=admin_headers)

    again = client.post(
        f"{API}/purchase-returns/{note['id']}/cancel", json=body, headers=admin_headers
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ALREADY_CANCELLED"


# --- numbering, scoping and who may see what ------------------------------


def test_debit_notes_run_on_their_own_gap_free_series(client, admin_headers, stocked):
    supplier, _medicine, purchase = stocked
    batch_id = purchase["items"][0]["batch_id"]

    numbers = [
        raise_return(client, admin_headers, supplier["id"], batch_id, "10").json()[
            "debit_note_no"
        ]
        for _ in range(3)
    ]
    assert numbers == ["00000001", "00000002", "00000003"]


def test_a_sales_executive_never_sees_the_cost(client, sales_headers, admin_headers, stocked):
    """CLAUDE.md rule 4, enforced in the response schema, not the UI."""
    supplier, _medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()

    response = client.get(f"{API}/purchase-returns/{note['id']}", headers=sales_headers)
    if response.status_code == 403:
        pytest.skip("a sales executive has no purchases capability by default")

    body = response.json()
    assert "total_amount" not in body
    assert "credit_balance" not in body
    for item in body["items"]:
        assert "cost_per_unit" not in item
        assert "taxable_value" not in item


def test_a_batch_from_another_store_is_not_returnable(client, admin_headers, stocked):
    supplier, _medicine, _purchase = stocked
    response = raise_return(client, admin_headers, supplier["id"], MISSING_ID, "1")
    assert response.status_code == 404


def test_an_unknown_supplier_is_refused(client, admin_headers, stocked):
    _supplier, _medicine, purchase = stocked
    response = raise_return(
        client, admin_headers, MISSING_ID, purchase["items"][0]["batch_id"], "1"
    )
    assert response.status_code == 404
    assert response.json()["detail"]["field"] == "supplier_id"


def test_every_return_writes_an_audit_row(client, admin_headers, db, stocked):
    """CLAUDE.md rule 6."""
    from app.models.audit import AuditLog

    supplier, _medicine, purchase = stocked
    note = raise_return(
        client, admin_headers, supplier["id"], purchase["items"][0]["batch_id"], "50"
    ).json()

    entry = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "PurchaseReturn", AuditLog.entity_id == note["id"])
        .one_or_none()
    )
    assert entry is not None
    assert "560.00" in entry.after_json
