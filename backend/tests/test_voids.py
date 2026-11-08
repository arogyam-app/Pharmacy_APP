"""The void workflow: request, approve, reject.

The control being tested is the separation: whoever billed a sale cannot
also erase it. A sales executive may only ask.
"""

from decimal import Decimal

from tests.conftest import API, sell, stocked_medicine


def line_of(medicine_id, qty="1", **kw):
    return {"medicine_id": medicine_id, "qty_units": qty, **kw}


REASON = "Customer changed their mind before leaving the counter"


def test_a_sales_exec_can_request_a_void_but_not_grant_it(
    client, admin_headers, sales_headers
):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, sales_headers, [line_of(medicine["id"], "10", discount_pct="0")])

    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=sales_headers,
    )
    assert requested.status_code == 201
    assert requested.json()["status"] == "PENDING"

    # The sale is flagged, not cancelled.
    after = client.get(f"{API}/sales/{sale['id']}", headers=sales_headers).json()
    assert after["status"] == "VOID_REQUESTED"

    # And the exec cannot approve their own request.
    forbidden = client.post(
        f"{API}/void-requests/{requested.json()['id']}/approve", json={},
        headers=sales_headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "FORBIDDEN_ROLE"


def test_requesting_a_void_needs_a_real_reason(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    response = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": "no"},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_approving_a_void_puts_every_unit_back(client, admin_headers, sales_headers, db):
    """The roadmap's Phase 5 criterion: a voided sale restores stock."""
    from app.models.inventory import Batch

    medicine, batch = stocked_medicine(client, admin_headers)     # 100 units
    sale = sell(client, sales_headers, [line_of(medicine["id"], "30", discount_pct="0")])

    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == Decimal("70.000")

    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=sales_headers,
    ).json()
    approved = client.post(
        f"{API}/void-requests/{requested['id']}/approve",
        json={"review_note": "Agreed"},
        headers=admin_headers,
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == Decimal("100.000")

    voided = client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()
    assert voided["status"] == "VOIDED"


def test_a_void_reverses_through_the_ledger_not_by_rewriting_it(
    client, admin_headers, db
):
    from sqlalchemy import func

    from app.models.inventory import Batch, StockLedger

    medicine, batch = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "12", discount_pct="0")])
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()
    client.post(f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers)

    ledger = client.get(
        f"{API}/inventory/ledger", params={"batch_id": batch["id"]}, headers=admin_headers
    ).json()
    kinds = [row["movement_type"] for row in ledger["items"]]
    assert "VOID_REVERSAL" in kinds
    assert "SALE" in kinds          # the original movement is still there

    db.expire_all()
    row = db.get(Batch, batch["id"])
    ledger_sum = (
        db.query(func.sum(StockLedger.qty_units_delta))
        .filter(StockLedger.batch_id == row.id)
        .scalar()
    )
    assert row.qty_available_units == ledger_sum


def test_a_void_does_not_inflate_the_quantity_received(client, admin_headers, db):
    """Stock coming back is not a fresh consignment."""
    from app.models.inventory import Batch

    medicine, batch = stocked_medicine(client, admin_headers)
    before = db.get(Batch, batch["id"]).qty_received_units

    sale = sell(client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")])
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()
    client.post(f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers)

    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_received_units == before


def test_the_invoice_number_is_kept_and_never_reissued(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="50")
    first = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    requested = client.post(
        f"{API}/sales/{first['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()
    client.post(f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers)

    voided = client.get(f"{API}/sales/{first['id']}", headers=admin_headers).json()
    assert voided["invoice_no"] == "10000001"

    second = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    assert second["invoice_no"] == "10000002"       # the number is not recycled


def test_rejecting_a_void_leaves_the_invoice_standing(client, admin_headers, db):
    from app.models.inventory import Batch

    medicine, batch = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")])
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()

    rejected = client.post(
        f"{API}/void-requests/{requested['id']}/reject",
        json={"review_note": "Goods already left the shop"},
        headers=admin_headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert rejected.json()["review_note"] == "Goods already left the shop"

    back = client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()
    assert back["status"] == "COMPLETED"

    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == Decimal("90.000")


def test_a_rejection_needs_a_note(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()

    response = client.post(
        f"{API}/void-requests/{requested['id']}/reject", json={}, headers=admin_headers
    )
    assert response.status_code == 422


def test_a_second_request_while_one_is_pending_is_refused(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    )
    again = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "VOID_ALREADY_REQUESTED"


def test_an_already_void_sale_cannot_be_voided_again(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()
    client.post(f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers)

    again = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "SALE_ALREADY_VOIDED"


def test_reviewing_the_same_request_twice_is_refused(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()

    client.post(f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers)
    again = client.post(
        f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "VOID_ALREADY_REVIEWED"


def test_a_sale_with_returns_cannot_be_voided(client, admin_headers):
    """Voiding would put the same stock back a second time."""
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(
        client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")],
        customer={"mobile": "9876500001"},
    )
    client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": "2"}],
            "refund_mode": "CASH",
        },
        headers=admin_headers,
    )

    response = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SALE_HAS_RETURNS"


def test_the_pending_queue_is_admin_only(client, admin_headers, sales_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, sales_headers, [line_of(medicine["id"], "1")])
    client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=sales_headers,
    )

    assert client.get(f"{API}/void-requests", headers=sales_headers).status_code == 403

    queue = client.get(f"{API}/void-requests", headers=admin_headers).json()
    assert queue["total"] == 1
    row = queue["items"][0]
    assert row["invoice_no"] == "10000001"
    assert row["requested_by_name"] == "Test Sales"
    assert row["request_reason"] == REASON


def test_both_sides_of_the_decision_are_audited(client, admin_headers, db):
    from app.models.audit import AuditLog

    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    requested = client.post(
        f"{API}/sales/{sale['id']}/void-request", json={"reason": REASON},
        headers=admin_headers,
    ).json()
    client.post(f"{API}/void-requests/{requested['id']}/approve", json={}, headers=admin_headers)

    actions = {
        row.action for row in db.query(AuditLog).filter(AuditLog.entity_type == "Sale")
    }
    assert {"CREATE", "VOID", "APPROVE"} <= actions
