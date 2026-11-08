"""Confirming a sale: stock, invoice numbers, payments, idempotency.

The phase's acceptance criterion lives here — a sale of one tablet plus two
strips of another medicine, with a discount, prints correctly and drops
stock by the exact unit count.
"""

from app.utils.clock import business_date
from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import API, make_batch, make_medicine, sell, stocked_medicine


def line_of(medicine_id, qty="1", **kw):
    return {"medicine_id": medicine_id, "qty_units": qty, **kw}


# --- the acceptance case --------------------------------------------------


def test_one_tablet_plus_two_strips_with_a_discount(client, admin_headers, db):
    """The roadmap's Phase 4 acceptance test, end to end."""
    from app.models.inventory import Batch

    single, single_batch = stocked_medicine(client, admin_headers, mrp="100.00")
    strips, strips_batch = stocked_medicine(
        client, admin_headers, mrp="250.00", brand_name="Testolol 25"
    )

    sale = sell(
        client,
        admin_headers,
        [
            line_of(single["id"], "1", discount_pct="0"),      # one tablet
            line_of(strips["id"], "20", discount_pct="10"),    # two strips of ten
        ],
    )

    assert len(sale["items"]) == 2
    assert sale["invoice_no"] == "10000001"
    assert sale["status"] == "COMPLETED"

    tablet = sale["items"][0]
    assert tablet["qty_units"] == "1.000"
    assert tablet["net_amount"] == "10.00"

    two_strips = sale["items"][1]
    assert two_strips["qty_units"] == "20.000"
    assert two_strips["gross_amount"] == "500.00"     # 20 x 25.00
    assert two_strips["discount_amount"] == "50.00"
    assert two_strips["net_amount"] == "450.00"

    # Stock drops by the exact unit count, not by packs.
    db.expire_all()
    assert db.get(Batch, single_batch["id"]).qty_available_units == Decimal("99.000")
    assert db.get(Batch, strips_batch["id"]).qty_available_units == Decimal("80.000")

    assert sale["amount_in_words"].lower().startswith("rupees")


# --- stock and the ledger -------------------------------------------------


def test_the_ledger_still_equals_available_after_a_sale(client, admin_headers, db):
    """CLAUDE.md rule 3, across the billing path."""
    from sqlalchemy import func

    from app.models.inventory import Batch, StockLedger

    medicine, _ = stocked_medicine(client, admin_headers)
    sell(client, admin_headers, [line_of(medicine["id"], "7")])

    db.expire_all()
    for batch in db.query(Batch).all():
        ledger_sum = (
            db.query(func.sum(StockLedger.qty_units_delta))
            .filter(StockLedger.batch_id == batch.id)
            .scalar()
        )
        assert batch.qty_available_units == ledger_sum


def test_a_sale_writes_a_ledger_row_pointing_at_the_line(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "3")])

    ledger = client.get(
        f"{API}/inventory/ledger", params={"medicine_id": medicine["id"]},
        headers=admin_headers,
    ).json()
    entry = ledger["items"][0]

    assert entry["movement_type"] == "SALE"
    assert entry["qty_units_delta"] == "-3.000"
    assert entry["reference_type"] == "SALE_ITEM"
    assert entry["reference_id"] == sale["items"][0]["id"]
    assert sale["invoice_no"] in entry["reason"]


def test_a_split_line_gives_each_batch_its_own_sale_item(client, admin_headers, db):
    from app.models.inventory import Batch

    medicine = make_medicine(client, admin_headers)
    first = make_batch(
        client, admin_headers, medicine["id"], batch_no="A", qty_packs="1",
        expiry_date=(business_date() + timedelta(days=100)).isoformat(),
    )
    second = make_batch(
        client, admin_headers, medicine["id"], batch_no="B", qty_packs="5",
        expiry_date=(business_date() + timedelta(days=400)).isoformat(),
    )

    sale = sell(client, admin_headers, [line_of(medicine["id"], "25")])

    assert len(sale["items"]) == 2
    assert {i["batch_no"] for i in sale["items"]} == {"A", "B"}

    db.expire_all()
    assert db.get(Batch, first["id"]).qty_available_units == Decimal("0.000")
    assert db.get(Batch, second["id"]).qty_available_units == Decimal("35.000")


def test_a_failed_sale_leaves_no_stock_moved_and_no_invoice(client, admin_headers, db):
    """The whole confirm runs in one transaction, or not at all."""
    from app.models.inventory import Batch
    from app.models.sale import Sale

    ok, ok_batch = stocked_medicine(client, admin_headers)
    short, _ = stocked_medicine(
        client, admin_headers, brand_name="Testolol 25", qty_packs="1"
    )
    before = db.get(Batch, ok_batch["id"]).qty_available_units

    response = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(ok["id"], "5"), line_of(short["id"], "500")],
            "payments": [{"mode": "CASH", "amount": "100.00"}],
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INSUFFICIENT_STOCK"

    db.expire_all()
    assert db.get(Batch, ok_batch["id"]).qty_available_units == before
    assert db.query(Sale).count() == 0


# --- invoice numbering ----------------------------------------------------


def test_invoice_numbers_are_sequential_gap_free_and_padded(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="50")

    numbers = [
        sell(client, admin_headers, [line_of(medicine["id"], "1")])["invoice_no"]
        for _ in range(3)
    ]
    assert numbers == ["10000001", "10000002", "10000003"]


def test_sale_and_purchase_numbers_run_on_separate_sequences(client, admin_headers):
    from tests.conftest import make_purchase, make_supplier

    medicine, _ = stocked_medicine(client, admin_headers)
    supplier = make_supplier(client, admin_headers)

    purchase = make_purchase(client, admin_headers, supplier["id"], medicine["id"])
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    assert purchase["doc_number"] == "00000001"
    assert sale["invoice_no"] == "10000001"      # its own counter, not shared


def test_the_invoice_series_opens_where_the_setting_says(client, admin_headers):
    """Sale invoices start high; the internal series do not.

    A shop moving off a paper book cannot start again at 1 beside numbers it
    has already handed to customers, so the invoice series opens clear of
    them. Returns, credit notes and purchases are internal documents nobody
    outside the shop has seen, and keep counting from 1.
    """
    from app.core.config import settings
    from app.models.enums import DocumentType
    from app.services.sequences import starting_number

    assert settings.invoice_number_start == 10_000_001
    assert starting_number(DocumentType.SALE) == 10_000_001

    for other in DocumentType:
        if other is not DocumentType.SALE:
            assert starting_number(other) == 1, other

    medicine, _ = stocked_medicine(client, admin_headers)
    first = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    assert first["invoice_no"] == "10000001"
    # Eight characters, because the column is CHAR(8) and the bill has room
    # for exactly that.
    assert len(first["invoice_no"]) == 8


# --- idempotency ----------------------------------------------------------


def test_a_repeated_submit_returns_the_same_invoice(client, admin_headers, db):
    from app.models.sale import Sale

    medicine, _ = stocked_medicine(client, admin_headers)
    payload = {
        "items": [line_of(medicine["id"], "5", discount_pct="0")],
        "payments": [{"mode": "CASH", "amount": "50.00"}],
        "idempotency_key": "till-1-20260801-0001",
    }

    first = client.post(f"{API}/sales", json=payload, headers=admin_headers)
    second = client.post(f"{API}/sales", json=payload, headers=admin_headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["invoice_no"] == first.json()["invoice_no"]
    assert any("already billed" in w for w in second.json()["warnings"])
    assert db.query(Sale).count() == 1


def test_a_double_submit_does_not_decrement_stock_twice(client, admin_headers, db):
    from app.models.inventory import Batch

    medicine, batch = stocked_medicine(client, admin_headers)
    payload = {
        "items": [line_of(medicine["id"], "10", discount_pct="0")],
        "payments": [{"mode": "CASH", "amount": "100.00"}],
        "idempotency_key": "double-click",
    }
    client.post(f"{API}/sales", json=payload, headers=admin_headers)
    client.post(f"{API}/sales", json=payload, headers=admin_headers)

    db.expire_all()
    assert db.get(Batch, batch["id"]).qty_available_units == Decimal("90.000")


def test_without_a_key_two_identical_carts_are_two_sales(client, admin_headers, db):
    """Two customers buying the same thing is normal, not a duplicate."""
    from app.models.sale import Sale

    medicine, _ = stocked_medicine(client, admin_headers)
    sell(client, admin_headers, [line_of(medicine["id"], "1")])
    sell(client, admin_headers, [line_of(medicine["id"], "1")])

    assert db.query(Sale).count() == 2


# --- payments -------------------------------------------------------------


def test_payments_must_add_up_to_the_payable(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)

    response = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "10", discount_pct="0")],
            "payments": [{"mode": "CASH", "amount": "50.00"}],   # invoice is 100
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "PAYMENT_MISMATCH"
    assert "100" in detail["message"]


def test_a_split_payment_is_recorded_mode_by_mode(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)

    sale = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "10", discount_pct="0")],
            "payments": [
                {"mode": "CASH", "amount": "40.00"},
                {"mode": "UPI", "amount": "60.00", "reference_no": "UPI-99887"},
            ],
        },
        headers=admin_headers,
    ).json()

    assert len(sale["payments"]) == 2
    assert sale["paid_amount"] == "100.00"
    upi = [p for p in sale["payments"] if p["mode"] == "UPI"][0]
    assert upi["reference_no"] == "UPI-99887"


def test_a_credit_note_payment_must_name_the_note(client, admin_headers):
    """Redemption itself is covered in tests/test_credit_notes.py."""
    medicine, _ = stocked_medicine(client, admin_headers)

    response = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "10", discount_pct="0")],
            "payments": [{"mode": "CREDIT_NOTE", "amount": "100.00"}],
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_REQUIRED"


# --- customers ------------------------------------------------------------


def test_a_new_mobile_quick_creates_the_customer(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)

    sale = sell(
        client, admin_headers, [line_of(medicine["id"], "1")],
        customer={"mobile": "9876543210", "name": "R Banerjee"},
    )
    assert sale["customer_id"] is not None
    assert sale["customer_mobile"] == "9876543210"

    found = client.get(f"{API}/customers/by-mobile/9876543210", headers=admin_headers)
    assert found.status_code == 200
    assert found.json()["name"] == "R Banerjee"
    assert found.json()["sale_count"] == 1


def test_a_returning_customer_is_matched_not_duplicated(client, admin_headers, db):
    from app.models.party import Customer

    medicine, _ = stocked_medicine(client, admin_headers)
    for _ in range(2):
        sell(
            client, admin_headers, [line_of(medicine["id"], "1")],
            customer={"mobile": "9876543210"},
        )

    assert db.query(Customer).filter(Customer.mobile == "9876543210").count() == 1


def test_a_walk_in_sale_needs_no_customer_at_all(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)

    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    assert sale["customer_id"] is None
    assert sale["customer_mobile"] is None


# --- the register ---------------------------------------------------------


def test_the_register_finds_a_sale_by_invoice_number_or_mobile(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, qty_packs="50")

    first = sell(
        client, admin_headers, [line_of(medicine["id"], "1")],
        customer={"mobile": "9800011111"},
    )
    sell(
        client, admin_headers, [line_of(medicine["id"], "1")],
        customer={"mobile": "9800022222"},
    )

    by_invoice = client.get(
        f"{API}/sales", params={"q": first["invoice_no"]}, headers=admin_headers
    ).json()
    assert by_invoice["total"] == 1

    by_mobile = client.get(
        f"{API}/sales", params={"q": "9800022222"}, headers=admin_headers
    ).json()
    assert by_mobile["total"] == 1
    assert by_mobile["items"][0]["customer_mobile"] == "9800022222"


def test_a_sale_is_reachable_by_its_invoice_number(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    found = client.get(
        f"{API}/sales/by-invoice/{sale['invoice_no']}", headers=admin_headers
    )
    assert found.status_code == 200
    assert found.json()["id"] == sale["id"]


def test_a_sales_exec_can_bill_and_see_the_register(client, admin_headers, sales_headers):
    """The counter is their job; the register is how they find a bill."""
    medicine, _ = stocked_medicine(client, admin_headers)

    sale = sell(client, sales_headers, [line_of(medicine["id"], "1")])
    assert sale["sales_user_name"] == "Test Sales"

    register = client.get(f"{API}/sales", headers=sales_headers).json()
    assert register["total"] == 1


# --- cost gating ----------------------------------------------------------


def test_margin_is_admin_only_on_a_sale_line(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(
        client, admin_headers, medicine["id"], mrp_per_pack="100.00", cost_per_pack="60.00"
    )
    sale = sell(client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")])

    as_admin = client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()
    line = as_admin["items"][0]
    assert line["unit_cost"] == "6.0000"
    assert line["line_cost"] == "60.00"
    assert line["margin_amount"] == "40.00"

    as_sales = client.get(f"{API}/sales/{sale['id']}", headers=sales_headers).json()
    line = as_sales["items"][0]
    assert "unit_cost" not in line
    assert "line_cost" not in line
    assert "margin_amount" not in line
    assert line["net_amount"] == "100.00"


def test_the_cost_snapshot_survives_a_later_price_change(client, admin_headers):
    """Margin reports must reflect what the sale actually earned."""
    medicine = make_medicine(client, admin_headers)
    batch = make_batch(
        client, admin_headers, medicine["id"], mrp_per_pack="100.00", cost_per_pack="60.00"
    )
    sale = sell(client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")])

    client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"cost_per_pack": "90.00"},
        headers=admin_headers,
    )
    after = client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()
    assert after["items"][0]["unit_cost"] == "6.0000"       # unchanged


# --- immutability ---------------------------------------------------------


def test_there_is_no_way_to_delete_a_sale(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    response = client.delete(f"{API}/sales/{sale['id']}", headers=admin_headers)
    assert response.status_code == 405


def test_snapshots_keep_an_invoice_readable_after_the_catalogue_changes(
    client, admin_headers
):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    client.patch(
        f"{API}/medicines/{medicine['id']}",
        json={"brand_name": "Renamed Entirely"},
        headers=admin_headers,
    )
    after = client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()
    assert after["items"][0]["medicine_name"] == "Testolol 50"


# --- printing -------------------------------------------------------------


def test_the_print_payload_carries_the_store_header_and_tax_summary(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "10", discount_pct="0")])

    payload = client.get(f"{API}/sales/{sale['id']}/print", headers=admin_headers).json()

    assert payload["store"]["name"] == "Test Pharmacy"
    assert payload["is_reprint"] is False
    assert payload["amount_in_words"].lower().startswith("rupees")

    summary = payload["tax_summary"]
    assert len(summary) == 1
    assert summary[0]["gst_rate"] == "12.00"
    assert Decimal(summary[0]["total_tax"]) == Decimal(sale["cgst_amount"]) + Decimal(
        sale["sgst_amount"]
    )


def test_the_tax_summary_groups_by_rate(client, admin_headers):
    twelve, _ = stocked_medicine(client, admin_headers)
    five = make_medicine(client, admin_headers, brand_name="Testolol 25")
    make_batch(client, admin_headers, five["id"], gst_rate="5")

    sale = sell(
        client, admin_headers,
        [line_of(twelve["id"], "10", discount_pct="0"),
         line_of(five["id"], "10", discount_pct="0")],
    )
    summary = client.get(
        f"{API}/sales/{sale['id']}/print", headers=admin_headers
    ).json()["tax_summary"]

    assert [r["gst_rate"] for r in summary] == ["5.00", "12.00"]


def test_a_second_print_is_recorded_as_a_reprint(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    first = client.post(f"{API}/sales/{sale['id']}/printed", headers=admin_headers).json()
    second = client.post(f"{API}/sales/{sale['id']}/printed", headers=admin_headers).json()

    assert first["is_reprint"] is False   # the original
    assert first["reprint_count"] == 1
    assert second["is_reprint"] is True
    assert second["reprint_count"] == 2


def test_opening_a_bill_on_screen_counts_nothing(client, admin_headers):
    """Reading an invoice is not printing it — the register counts sheets."""
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    for _ in range(3):
        payload = client.get(f"{API}/sales/{sale['id']}/print", headers=admin_headers).json()
        assert payload["is_reprint"] is False
        assert payload["reprint_count"] == 0

    row = client.get(f"{API}/sales/{sale['id']}", headers=admin_headers).json()
    assert row["reprint_count"] == 0


def test_a_recorded_print_shows_on_the_next_read(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])

    client.post(f"{API}/sales/{sale['id']}/printed", headers=admin_headers)

    payload = client.get(f"{API}/sales/{sale['id']}/print", headers=admin_headers).json()
    assert payload["is_reprint"] is True     # it has been printed before
    assert payload["reprint_count"] == 1
    assert client.get(
        f"{API}/sales/{sale['id']}", headers=admin_headers
    ).json()["reprint_count"] == 1


def test_recording_a_print_writes_an_audit_row(client, admin_headers, db):
    from app.models.audit import AuditLog

    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    client.post(f"{API}/sales/{sale['id']}/printed", headers=admin_headers)

    entry = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "Sale", AuditLog.entity_id == sale["id"],
                AuditLog.action == "PRINT")
        .one()
    )
    assert entry.after_json is not None


# --- audit ----------------------------------------------------------------


def test_a_sale_and_every_print_are_audited(client, admin_headers, db):
    from app.models.audit import AuditLog

    medicine, _ = stocked_medicine(client, admin_headers)
    sale = sell(client, admin_headers, [line_of(medicine["id"], "1")])
    client.post(f"{API}/sales/{sale['id']}/printed", headers=admin_headers)

    actions = {
        row.action
        for row in db.query(AuditLog).filter(AuditLog.entity_type == "Sale")
    }
    assert {"CREATE", "PRINT"} <= actions


def test_a_discount_override_is_recorded_on_the_invoice_and_the_audit(
    client, admin_headers, db
):
    from app.models.audit import AuditLog

    medicine, _ = stocked_medicine(client, admin_headers)
    reason = "Damaged carton, agreed with the customer"

    sale = sell(
        client, admin_headers,
        [line_of(medicine["id"], "10", discount_pct="35")],
        discount_override_reason=reason,
    )
    assert sale["discount_override_reason"] == reason
    assert sale["items"][0]["discount_pct"] == "35.00"

    row = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "Sale", AuditLog.action == "CREATE")
        .one()
    )
    assert row.reason == reason
