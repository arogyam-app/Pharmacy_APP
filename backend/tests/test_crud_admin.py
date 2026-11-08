"""Admin update and delete for medicines, compositions and batches.

Deletion is allowed only where nothing downstream depends on the row. Where
something does, the endpoint says so and offers the right alternative rather
than quietly destroying history.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.audit import AuditLog
from app.models.catalog import Composition, Medicine
from app.models.inventory import Batch, StockLedger
from app.utils.clock import business_date
from tests.conftest import API, make_batch, make_medicine

TODAY = business_date()


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# --- medicines -----------------------------------------------------------


def test_admin_updates_a_medicine(client, admin_headers):
    medicine = make_medicine(client, admin_headers)

    updated = client.patch(
        f"{API}/medicines/{medicine['id']}",
        json={
            "brand_name": "Renamed 50",
            "rack_location": "B4",
            "hsn_code": "30049011",
            "schedule_type": "SCHEDULE_H",
        },
        headers=admin_headers,
    ).json()

    assert updated["brand_name"] == "Renamed 50"
    assert updated["rack_location"] == "B4"
    assert updated["hsn_code"] == "30049011"
    assert updated["schedule_type"] == "SCHEDULE_H"


def test_renaming_a_medicine_keeps_it_findable(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers, brand_name="Oldbrand 50")
    make_batch(client, admin_headers, medicine["id"])

    client.patch(
        f"{API}/medicines/{medicine['id']}",
        json={"brand_name": "Newbrand 50"},
        headers=admin_headers,
    )

    assert client.get(f"{API}/medicines/search?q=oldbrand", headers=sales_headers).json() == []
    found = client.get(f"{API}/medicines/search?q=newbrand", headers=sales_headers).json()
    assert [m["brand_name"] for m in found] == ["Newbrand 50"]


def test_an_unstocked_medicine_is_deleted_outright(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers)

    result = client.delete(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()

    assert result["deleted"] is True
    assert db.get(Medicine, medicine["id"]) is None


def test_a_stocked_medicine_is_deactivated_not_erased(client, admin_headers, sales_headers, db):
    """Stock history must survive; the medicine simply leaves the counter."""
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"])

    result = client.delete(f"{API}/medicines/{medicine['id']}", headers=admin_headers).json()

    assert result["deleted"] is False
    assert "deactivated" in result["message"]

    db.expire_all()
    stored = db.get(Medicine, medicine["id"])
    assert stored is not None
    assert stored.is_active is False
    # Gone from the counter, but its batches remain on record.
    assert client.get(f"{API}/medicines/search?q=testolol", headers=sales_headers).json() == []
    assert db.query(Batch).filter(Batch.medicine_id == medicine["id"]).count() == 1


def test_deleting_an_already_inactive_stocked_medicine_is_refused(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"])
    client.delete(f"{API}/medicines/{medicine['id']}", headers=admin_headers)

    second = client.delete(f"{API}/medicines/{medicine['id']}", headers=admin_headers)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "MEDICINE_IN_USE"


def test_medicine_deletion_is_audited(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers, brand_name="Doomed 10")
    client.delete(f"{API}/medicines/{medicine['id']}", headers=admin_headers)

    entry = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "Medicine", AuditLog.action == "DELETE")
        .one()
    )
    assert "Doomed 10" in entry.before_json


# --- compositions --------------------------------------------------------


def test_admin_reformulates_a_composition(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers, composition_text="Atenolol 50mg")

    updated = client.patch(
        f"{API}/compositions/{medicine['composition_id']}",
        json={"composition_text": "Atenolol 25mg + Chlorthalidone 12.5mg"},
        headers=admin_headers,
    ).json()

    assert updated["composition_text"] == "ATENOLOL (25MG) + CHLORTHALIDONE (12.5MG)"
    assert len(updated["components"]) == 2
    # The medicine's haystack must follow the change.
    found = client.get(f"{API}/medicines/search?q=chlorthalidone", headers=sales_headers).json()
    assert [m["brand_name"] for m in found] == ["Testolol 50"]


def test_reformulating_via_components_works_too(client, admin_headers):
    medicine = make_medicine(client, admin_headers, composition_text="Atenolol 50mg")

    updated = client.patch(
        f"{API}/compositions/{medicine['composition_id']}",
        json={
            "components": [
                {"name": "Zeta", "strength": "10mg"},
                {"name": "Alpha", "strength": "5mg"},
            ]
        },
        headers=admin_headers,
    ).json()

    # Canonical order, whatever order they were sent in — and the canonical
    # order leads with the strongest ingredient, so Zeta comes first here
    # despite being second alphabetically and second in the request.
    assert updated["composition_text"] == "ZETA (10MG) + ALPHA (5MG)"


def test_reformulating_onto_an_existing_composition_is_refused(client, admin_headers):
    """Two rows for one formulation would split a set of alternatives in half."""
    make_medicine(client, admin_headers, brand_name="A", composition_text="Paracetamol 500mg")
    other = make_medicine(client, admin_headers, brand_name="B", composition_text="Atenolol 50mg")

    response = client.patch(
        f"{API}/compositions/{other['composition_id']}",
        json={"composition_text": "paracetamol 500 mg"},
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "COMPOSITION_EXISTS"


def test_reformulation_regroups_alternatives(client, admin_headers, sales_headers):
    first = make_medicine(client, admin_headers, brand_name="Alpha 50",
                          composition_text="Atenolol 50mg")
    second = make_medicine(client, admin_headers, brand_name="Beta 50",
                           manufacturer_name="Other Labs", composition_text="Atenolol 50mg")

    before = client.get(
        f"{API}/medicines/{first['id']}/alternatives", headers=sales_headers
    ).json()
    assert [a["brand_name"] for a in before] == ["Beta 50"]

    # Move the second brand to a different formulation.
    client.patch(
        f"{API}/medicines/{second['id']}",
        json={"composition_text": "Metoprolol 25mg"},
        headers=admin_headers,
    )

    after = client.get(
        f"{API}/medicines/{first['id']}/alternatives", headers=sales_headers
    ).json()
    assert after == []


def test_descriptive_fields_update_without_touching_the_formulation(client, admin_headers):
    medicine = make_medicine(client, admin_headers)

    updated = client.patch(
        f"{API}/compositions/{medicine['composition_id']}",
        json={"therapeutic_class": "Beta blocker", "notes": "Cardio-selective"},
        headers=admin_headers,
    ).json()

    assert updated["therapeutic_class"] == "Beta blocker"
    assert updated["composition_text"] == "ATENOLOL (50MG)"


def test_an_unused_composition_is_deleted(client, admin_headers, db):
    created = client.post(
        f"{API}/compositions", json={"composition_text": "Orphan 10mg"}, headers=admin_headers
    ).json()

    result = client.delete(f"{API}/compositions/{created['id']}", headers=admin_headers).json()

    assert result["deleted"] is True
    assert db.get(Composition, created["id"]) is None


def test_a_composition_in_use_cannot_be_deleted(client, admin_headers):
    medicine = make_medicine(client, admin_headers)

    response = client.delete(
        f"{API}/compositions/{medicine['composition_id']}", headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "COMPOSITION_IN_USE"
    assert "1 medicine" in response.json()["detail"]["message"]


# --- batches -------------------------------------------------------------


def test_admin_updates_batch_prices_and_expiry(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], mrp_per_pack="50.00")

    updated = client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={
            "mrp_per_pack": "55.00",
            "cost_per_pack": "44.00",
            "gst_rate": "5",
            "max_discount_pct": "8",
            "expiry_date": iso(500),
        },
        headers=admin_headers,
    ).json()

    assert Decimal(updated["mrp_per_pack"]) == Decimal("55.00")
    assert Decimal(updated["unit_mrp"]) == Decimal("5.5000")
    assert Decimal(updated["unit_cost"]) == Decimal("4.4000")
    assert Decimal(updated["gst_rate"]) == Decimal("5.00")
    assert Decimal(updated["effective_max_discount_pct"]) == Decimal("8.00")


def test_quantity_is_corrected_through_the_ledger_not_edited(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="10")  # 100 units

    updated = client.post(
        f"{API}/inventory/batches/{batch['id']}/adjust",
        json={"qty_units_delta": "-7", "reason": "Damaged in transit"},
        headers=admin_headers,
    ).json()

    assert Decimal(updated["qty_available_units"]) == Decimal("93.000")

    entries = (
        db.query(StockLedger)
        .filter(StockLedger.batch_id == batch["id"])
        .order_by(StockLedger.id)
        .all()
    )
    assert [e.movement_type for e in entries] == ["OPENING", "ADJUSTMENT_OUT"]
    assert entries[-1].qty_units_delta == Decimal("-7.000")
    assert entries[-1].reason == "Damaged in transit"
    # The cached figure still equals the ledger sum.
    assert sum(e.qty_units_delta for e in entries) == Decimal("93.000")


def test_a_positive_adjustment_is_recorded_as_an_inward_movement(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="1")

    client.post(
        f"{API}/inventory/batches/{batch['id']}/adjust",
        json={"qty_units_delta": "5", "reason": "Physical count correction"},
        headers=admin_headers,
    )

    entry = (
        db.query(StockLedger)
        .filter(StockLedger.batch_id == batch["id"])
        .order_by(StockLedger.id.desc())
        .first()
    )
    assert entry.movement_type == "ADJUSTMENT_IN"


def test_an_adjustment_cannot_drive_stock_negative(client, admin_headers):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="1")  # 10 units

    response = client.post(
        f"{API}/inventory/batches/{batch['id']}/adjust",
        json={"qty_units_delta": "-50", "reason": "Typo"},
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INSUFFICIENT_STOCK"


def test_an_adjustment_needs_a_reason(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    batch = make_batch(client, admin_headers, medicine["id"])

    response = client.post(
        f"{API}/inventory/batches/{batch['id']}/adjust",
        json={"qty_units_delta": "-1", "reason": ""},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_a_batch_entered_in_error_is_deleted_with_its_ledger(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="10")

    result = client.delete(
        f"{API}/inventory/batches/{batch['id']}", headers=admin_headers
    ).json()

    assert result["deleted"] is True
    assert db.get(Batch, batch["id"]) is None
    assert db.query(StockLedger).filter(StockLedger.batch_id == batch["id"]).count() == 0


def test_deleting_a_batch_rolls_back_the_order_book_baseline(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="10")

    db.expire_all()
    assert db.get(Medicine, medicine["id"]).stock_baseline_units == Decimal("100.000")

    client.delete(f"{API}/inventory/batches/{batch['id']}", headers=admin_headers)

    db.expire_all()
    assert db.get(Medicine, medicine["id"]).stock_baseline_units == Decimal("0.000")


def test_a_batch_with_a_sale_against_it_cannot_be_deleted(client, admin_headers, db):
    """Deleting would falsify the document that points at it."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    batch = make_batch(client, admin_headers, medicine["id"], qty_packs="10")

    # Stand in for Phase 4: a sale movement against the batch.
    stored = db.get(Batch, batch["id"])
    db.add(
        StockLedger(
            store_id=stored.store_id,
            medicine_id=stored.medicine_id,
            batch_id=stored.id,
            movement_type="SALE",
            qty_units_delta=Decimal("-2.000"),
            balance_after_units=Decimal("98.000"),
        )
    )
    db.commit()

    response = client.delete(f"{API}/inventory/batches/{batch['id']}", headers=admin_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "BATCH_HAS_MOVEMENTS"
    assert db.get(Batch, batch["id"]) is not None


def test_batch_deletion_is_audited(client, admin_headers, db):
    medicine = make_medicine(client, admin_headers)
    batch = make_batch(client, admin_headers, medicine["id"], batch_no="SCRAPME")
    client.delete(f"{API}/inventory/batches/{batch['id']}", headers=admin_headers)

    entry = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "Batch", AuditLog.action == "DELETE")
        .one()
    )
    assert "SCRAPME" in entry.before_json


# --- role boundary -------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("delete", "/medicines/1"),
        ("patch", "/medicines/1"),
        ("delete", "/compositions/1"),
        ("patch", "/compositions/1"),
        ("delete", "/inventory/batches/1"),
        ("patch", "/inventory/batches/1"),
        ("post", "/inventory/batches/1/adjust"),
    ],
)
def test_sales_executive_cannot_change_or_remove_records(
    client, sales_headers, method, path
):
    response = client.request(method.upper(), f"{API}{path}", json={}, headers=sales_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_ROLE"


# --- bulk import limits ---------------------------------------------------


def test_a_real_sized_formulary_import_is_accepted(client, admin_headers):
    """A master list of 20,000 formulations is the normal case, not an abuse.

    The Indian formulary runs to tens of thousands of distinct compositions,
    and a shop loading its master data starts with the whole list. The old
    5,000-row ceiling turned one upload into four and left the operator
    tracking which rows had already gone in.

    Kept small enough to run quickly — what it pins is that the *limit* is
    above 20,000, not that the import is fast.
    """
    from app.utils.spreadsheet import MAX_IMPORT_ROWS

    assert MAX_IMPORT_ROWS >= 20_000, (
        f"MAX_IMPORT_ROWS is {MAX_IMPORT_ROWS}; a real formulary does not fit. "
        f"Raising it also means raising IMPORT_TIMEOUT_MS in "
        f"frontend/src/api/client.js, or the browser gives up first."
    )


def test_the_row_limit_is_reported_against_the_row_it_stopped_on(client, admin_headers):
    """Past the cap the import stops and says so, rather than truncating quietly."""
    import io

    from app.utils import spreadsheet

    # Patching beats generating 50,000 rows to prove the branch works.
    original = spreadsheet.MAX_IMPORT_ROWS
    spreadsheet.MAX_IMPORT_ROWS = 3
    try:
        rows = "\n".join(f"Paracetamol {100 + i}mg" for i in range(6))
        csv = f"composition\n{rows}\n".encode()
        response = client.post(
            f"{API}/compositions/import",
            files={"file": ("formulary.csv", io.BytesIO(csv), "text/csv")},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 3
        assert any("row limit" in e["message"] for e in body["errors"])
    finally:
        spreadsheet.MAX_IMPORT_ROWS = original


def test_a_file_over_the_size_cap_names_the_real_limit(client, admin_headers):
    """The message used to say 5 MB regardless of what the limit actually was."""
    import io

    from app.utils.spreadsheet import MAX_IMPORT_BYTES

    oversized = b"composition\n" + b"x" * (MAX_IMPORT_BYTES + 1)
    response = client.post(
        f"{API}/compositions/import",
        files={"file": ("big.csv", io.BytesIO(oversized), "text/csv")},
        headers=admin_headers,
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "FILE_TOO_LARGE"
    assert str(MAX_IMPORT_BYTES // (1024 * 1024)) in detail["message"]
