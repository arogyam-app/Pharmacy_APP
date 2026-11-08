"""Adding items to an invoice already received."""
from datetime import timedelta
from decimal import Decimal

from app.models.inventory import Batch
from app.models.purchase import PurchaseInvoice
from app.utils.clock import business_date
from tests.conftest import API, make_batch, make_medicine, make_supplier

TODAY = business_date()


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _line(medicine_id, batch_no, **extra):
    line = {
        "medicine_id": medicine_id, "batch_no": batch_no, "expiry_date": iso(400),
        "qty_packs": "1", "cost_per_pack": "100", "mrp_per_pack": "150", "gst_rate": "12",
    }
    line.update(extra)
    return line


def _invoice(client, headers, supplier, medicine, no="INV-1"):
    response = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"], "supplier_invoice_no": no,
            "supplier_invoice_date": iso(0), "items": [_line(medicine["id"], "B1")],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_a_line_can_be_added_to_a_received_invoice(client, admin_headers, db):
    supplier = make_supplier(client, admin_headers)
    first = make_medicine(client, admin_headers, brand_name="Alpha")
    second = make_medicine(client, admin_headers, brand_name="Beta")
    invoice = _invoice(client, admin_headers, supplier, first)

    response = client.post(
        f"{API}/purchases/{invoice['id']}/items",
        json={"items": [_line(second["id"], "B2")]},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["items"]) == 2
    # The document keeps its identity.
    assert body["doc_number"] == invoice["doc_number"]
    assert body["supplier_invoice_no"] == invoice["supplier_invoice_no"]
    # And the new goods are on the shelf.
    batch = db.query(Batch).filter(Batch.batch_no == "B2").one()
    assert batch.qty_available_units > 0


def test_the_invoice_total_is_recomputed_not_appended_to(client, admin_headers, db):
    supplier = make_supplier(client, admin_headers)
    first = make_medicine(client, admin_headers, brand_name="Alpha")
    second = make_medicine(client, admin_headers, brand_name="Beta")
    invoice = _invoice(client, admin_headers, supplier, first)
    one_line_total = Decimal(invoice["total_amount"])

    body = client.post(
        f"{API}/purchases/{invoice['id']}/items",
        json={"items": [_line(second["id"], "B2")]},
        headers=admin_headers,
    ).json()

    # Two identical lines, so the document is worth twice one of them —
    # bar the rounding the store settings apply once, at the foot.
    assert Decimal(body["total_amount"]) > one_line_total
    stored = db.get(PurchaseInvoice, invoice["id"])
    assert stored.subtotal_taxable == sum(i.taxable_value for i in stored.items)
    assert stored.cgst_amount == sum(i.cgst_amount for i in stored.items)


def test_a_general_item_can_join_a_medicine_invoice(client, admin_headers):
    """The point of the change: two screens, two kinds, one document."""
    from tests.test_general_items import make_general

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    invoice = _invoice(client, admin_headers, supplier, medicine)
    soap = make_general(client, admin_headers)

    body = client.post(
        f"{API}/purchases/{invoice['id']}/items",
        json={
            "items": [
                _line(soap["id"], "S1", gst_rate="18", expiry_basis="NONE", expiry_date=None)
            ]
        },
        headers=admin_headers,
    ).json()

    rates = {Decimal(i["gst_rate"]) for i in body["items"]}
    assert rates == {Decimal("12.00"), Decimal("18.00")}


def test_a_cancelled_invoice_cannot_be_added_to(client, admin_headers):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    other = make_medicine(client, admin_headers, brand_name="Beta")
    invoice = _invoice(client, admin_headers, supplier, medicine)

    client.post(
        f"{API}/purchases/{invoice['id']}/cancel",
        json={"reason": "Keyed against the wrong supplier"},
        headers=admin_headers,
    )

    response = client.post(
        f"{API}/purchases/{invoice['id']}/items",
        json={"items": [_line(other["id"], "B9")]},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PURCHASE_CANCELLED"


def test_adding_items_is_audited_with_before_and_after(client, admin_headers, db):
    from app.models.audit import AuditLog

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    other = make_medicine(client, admin_headers, brand_name="Beta")
    invoice = _invoice(client, admin_headers, supplier, medicine)

    client.post(
        f"{API}/purchases/{invoice['id']}/items",
        json={"items": [_line(other["id"], "B2")]},
        headers=admin_headers,
    )

    entry = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "PurchaseInvoice", AuditLog.action == "UPDATE")
        .one()
    )
    assert entry.before_json is not None and entry.after_json is not None
    assert "item(s) added" in (entry.reason or "")


def test_a_sales_executive_without_receive_goods_cannot_add_items(
    client, admin_headers, sales_headers
):
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    invoice = _invoice(client, admin_headers, supplier, medicine)

    response = client.post(
        f"{API}/purchases/{invoice['id']}/items",
        json={"items": [_line(medicine["id"], "B2")]},
        headers=sales_headers,
    )
    assert response.status_code == 403


def test_the_register_row_says_which_tax_the_invoice_carries(client, admin_headers):
    """A client adding lines has to preview the same split the document has,
    rather than re-deriving it from the store's own state."""
    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    invoice = _invoice(client, admin_headers, supplier, medicine)

    rows = client.get(f"{API}/purchases", headers=admin_headers).json()["items"]
    row = next(r for r in rows if r["id"] == invoice["id"])

    assert row["is_intra_state"] == invoice["is_intra_state"]


def test_an_added_line_inherits_the_invoice_tax_split(client, admin_headers, db):
    """A line keyed on Tuesday is taxed the way the document was on Monday."""
    from app.models.purchase import PurchaseInvoice

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    other = make_medicine(client, admin_headers, brand_name="Beta")
    invoice = _invoice(client, admin_headers, supplier, medicine)

    # Flip the document to inter-state, as an out-of-state supplier's would be.
    stored = db.get(PurchaseInvoice, invoice["id"])
    stored.is_intra_state = False
    db.commit()

    body = client.post(
        f"{API}/purchases/{invoice['id']}/items",
        json={"items": [_line(other["id"], "B2")]},
        headers=admin_headers,
    ).json()

    added = next(i for i in body["items"] if i["batch_no"] == "B2")
    assert Decimal(added["igst_amount"]) > 0
    assert Decimal(added["cgst_amount"]) == 0
    assert Decimal(added["sgst_amount"]) == 0
