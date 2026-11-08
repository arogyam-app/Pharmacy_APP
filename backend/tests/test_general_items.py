"""General (non-medicine) stock: soap, nappies, a thermometer.

They live in the same catalogue as medicines, because everything operational
about them is the same — a batch, an MRP, a GST rate, a line on the same
invoice — and only the pharmacological attributes differ. These tests pin
the three things that follow from that: a general item has no formulation, a
pack of twelve is twelve sellable articles, and a shelf life can be stated
three different ways.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.models.enums import NO_EXPIRY
from app.models.inventory import Batch
from app.utils.clock import business_date
from tests.conftest import API, make_batch, make_medicine, make_purchase, make_supplier, sell


def line_of(medicine_id, qty="1", **kw):
    return {"medicine_id": medicine_id, "qty_units": qty, **kw}


TODAY = business_date()


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def make_general(client, headers, **overrides):
    """A general item: no composition, no schedule, sold by the piece."""
    payload = {
        "brand_name": "Dettol Soap",
        "manufacturer_name": "Reckitt",
        "item_type": "GENERAL",
        "pack_size": "12",
        "pack_unit": "PIECE",
    }
    payload.update(overrides)
    response = client.post(f"{API}/medicines", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# --- the catalogue -------------------------------------------------------


def test_a_general_item_needs_no_composition(client, admin_headers):
    item = make_general(client, admin_headers)

    assert item["item_type"] == "GENERAL"
    assert item["composition_id"] is None
    assert item["composition_text"] is None
    # Nothing pharmacological survives, whatever the client sent.
    assert item["dosage_form"] == "GENERAL"
    assert item["schedule_type"] == "OTC"
    assert item["is_prescription_required"] is False


def test_a_medicine_still_requires_a_composition(client, admin_headers):
    response = client.post(
        f"{API}/medicines",
        json={"brand_name": "Nameless", "manufacturer_name": "Test Labs"},
        headers=admin_headers,
    )
    assert response.status_code == 422


def test_a_general_item_cannot_be_given_a_schedule(client, admin_headers):
    """Sent anyway, it is normalised rather than rejected — there is nothing
    to check a prescription against."""
    item = make_general(
        client, admin_headers, schedule_type="SCHEDULE_H", is_prescription_required=True
    )

    assert item["schedule_type"] == "OTC"
    assert item["is_prescription_required"] is False


def test_a_composition_sent_with_a_general_item_is_dropped(client, admin_headers):
    item = make_general(client, admin_headers, composition_text="PARACETAMOL (500MG)")
    assert item["composition_id"] is None


def test_a_box_of_twelve_soaps_is_twelve_sellable_units(client, admin_headers):
    """The point of the PIECE unit: sell one soap out of the box."""
    item = make_general(client, admin_headers, pack_size="12", pack_unit="PIECE")

    assert item["units_per_pack"] == 12
    assert item["is_countable"] is True


def test_a_general_item_measured_in_millilitres_is_one_unit(client, admin_headers):
    """A 500 ml bottle of cleaner is one bottle, not 500 sellable units —
    the same rule that stops the POS selling a millilitre of syrup."""
    item = make_general(
        client, admin_headers, brand_name="Floor Cleaner", pack_size="500", pack_unit="ML"
    )

    assert item["units_per_pack"] == 1
    assert item["is_countable"] is False


def test_a_general_item_has_no_alternatives(client, admin_headers):
    """Two general items are not substitutes just because neither has a salt."""
    first = make_general(client, admin_headers, brand_name="Soap A")
    make_general(client, admin_headers, brand_name="Soap B")

    response = client.get(f"{API}/medicines/{first['id']}/alternatives", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_products_can_be_filtered_by_kind(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    soap = make_general(client, admin_headers)

    general = client.get(f"{API}/medicines?item_type=GENERAL", headers=admin_headers).json()
    medicines = client.get(f"{API}/medicines?item_type=MEDICINE", headers=admin_headers).json()

    assert {r["id"] for r in general["items"]} == {soap["id"]}
    assert medicine["id"] in {r["id"] for r in medicines["items"]}
    assert soap["id"] not in {r["id"] for r in medicines["items"]}


def test_a_general_item_is_found_by_the_counter_search(client, admin_headers):
    make_general(client, admin_headers, brand_name="Dettol Soap")

    rows = client.get(f"{API}/medicines/search?q=dettol", headers=admin_headers).json()
    assert [r["brand_name"] for r in rows] == ["Dettol Soap"]
    assert rows[0]["composition_text"] is None


# --- the three ways a pack states its shelf life --------------------------


def test_mfg_date_and_best_before_compute_the_expiry(client, admin_headers):
    item = make_general(client, admin_headers)
    batch = make_batch(
        client, admin_headers, item["id"],
        expiry_basis="BEST_BEFORE",
        expiry_date=None,
        mfg_date=iso(-30),
        best_before_months=24,
    )

    expected = TODAY - timedelta(days=30)
    expected = expected.replace(year=expected.year + 2)
    assert batch["expiry_date"] == expected.isoformat()
    assert batch["has_expiry"] is True
    # The pack said "MFG …, best before 24 months" — keep both so a shelf
    # check can reconcile with what is printed.
    assert batch["mfg_date"] == iso(-30)
    assert batch["best_before_months"] == 24


def test_stock_that_never_expires_reports_no_expiry(client, admin_headers, db):
    item = make_general(client, admin_headers, brand_name="Comb")
    batch = make_batch(
        client, admin_headers, item["id"], expiry_basis="NONE", expiry_date=None
    )

    assert batch["has_expiry"] is False
    assert batch["is_expired"] is False
    assert batch["days_to_expiry"] is None
    # The sentinel is stored, never shown as a fact about the goods.
    assert db.get(Batch, batch["id"]).expiry_date == NO_EXPIRY


def test_a_medicine_still_needs_a_real_expiry(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    response = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": medicine["id"], "batch_no": "B1",
            "qty_packs": "5", "mrp_per_pack": "50.00",
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "EXPIRY_REQUIRED"


# --- batch numbers: mandatory for a medicine, optional for a general item -


def test_a_general_item_can_receive_stock_with_no_batch_number(client, admin_headers):
    """A comb has no lot number the shop tracks — a placeholder is
    generated rather than forcing the operator to invent one."""
    soap = make_general(client, admin_headers)
    batch = make_batch(client, admin_headers, soap["id"], batch_no="")

    assert batch["batch_no"].startswith("GEN-")


def test_a_medicine_still_requires_a_batch_number_on_direct_receipt(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    response = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": medicine["id"], "batch_no": "",
            "expiry_date": iso(400), "qty_packs": "10", "mrp_per_pack": "100.00",
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["field"] == "batch_no"


def test_a_general_item_can_be_purchased_with_no_batch_number(client, admin_headers):
    soap = make_general(client, admin_headers)
    supplier = make_supplier(client, admin_headers)

    purchase = make_purchase(
        client, admin_headers, supplier["id"], soap["id"], line={"batch_no": ""}
    )

    assert purchase["items"][0]["batch_no"].startswith("GEN-")


def test_a_medicine_still_requires_a_batch_number_on_purchase(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    supplier = make_supplier(client, admin_headers)

    response = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "SUP-900",
            "supplier_invoice_date": business_date().isoformat(),
            "items": [{
                "medicine_id": medicine["id"],
                "batch_no": "",
                "expiry_date": iso(400),
                "qty_packs": "10",
                "cost_per_pack": "100.00",
                "mrp_per_pack": "150.00",
                "gst_rate": "12",
            }],
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["field"] == "batch_no"


def test_a_best_before_period_needs_both_halves(client, admin_headers):
    item = make_general(client, admin_headers)
    response = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": item["id"], "batch_no": "B1", "qty_packs": "5",
            "mrp_per_pack": "50.00", "expiry_basis": "BEST_BEFORE",
            "mfg_date": iso(-10),
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "EXPIRY_REQUIRED"


def test_a_future_manufacturing_date_is_a_keying_error(client, admin_headers):
    item = make_general(client, admin_headers)
    response = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": item["id"], "batch_no": "B1", "qty_packs": "5",
            "mrp_per_pack": "50.00", "expiry_basis": "BEST_BEFORE",
            "mfg_date": iso(30), "best_before_months": 12,
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "EXPIRY_INVALID"


def test_non_expiring_stock_is_sellable_and_never_near_expiry(client, admin_headers):
    item = make_general(client, admin_headers, brand_name="Comb", pack_size="1")
    make_batch(
        client, admin_headers, item["id"], expiry_basis="NONE", expiry_date=None,
        qty_packs="10",
    )

    rows = client.get(f"{API}/medicines/search?q=comb", headers=admin_headers).json()
    assert rows[0]["in_stock"] is True
    assert rows[0]["has_near_expiry"] is False
    # No expiry to advertise, rather than a date in the year 9999.
    assert rows[0]["nearest_expiry"] is None


def test_non_expiring_stock_gets_its_own_ageing_bucket(client, admin_headers):
    item = make_general(client, admin_headers, brand_name="Comb", pack_size="1")
    make_batch(
        client, admin_headers, item["id"], expiry_basis="NONE", expiry_date=None,
        qty_packs="4",
    )

    body = client.get(
        f"{API}/reports/inventory/expiry-ageing", headers=admin_headers
    ).json()
    buckets = {row["bucket"]: row for row in body["rows"]}

    assert buckets["Does not expire"]["batch_count"] == 1
    assert buckets["Beyond 180 days"]["batch_count"] == 0


def test_switching_a_batch_to_never_expiring_clears_the_date(client, admin_headers, db):
    item = make_general(client, admin_headers)
    batch = make_batch(client, admin_headers, item["id"], expiry_date=iso(400))

    updated = client.patch(
        f"{API}/inventory/batches/{batch['id']}",
        json={"expiry_basis": "NONE"},
        headers=admin_headers,
    ).json()

    assert updated["has_expiry"] is False
    assert db.get(Batch, batch["id"]).expiry_date == NO_EXPIRY


# --- one bill, both kinds -------------------------------------------------


def test_a_medicine_and_a_general_item_bill_together(client, admin_headers):
    """The whole point: one customer, one basket, one invoice number."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(client, admin_headers, medicine["id"], mrp_per_pack="100.00", gst_rate="12")

    soap = make_general(client, admin_headers, pack_size="12")
    make_batch(
        client, admin_headers, soap["id"], batch_no="SOAP-1",
        mrp_per_pack="240.00", gst_rate="18",
        expiry_basis="BEST_BEFORE", expiry_date=None,
        mfg_date=iso(-60), best_before_months=36,
    )

    sale = sell(
        client, admin_headers,
        [line_of(medicine["id"], "10", discount_pct="0"),
         line_of(soap["id"], "2", discount_pct="0")],
    )

    assert len(sale["items"]) == 2
    by_name = {item["medicine_name"]: item for item in sale["items"]}
    assert Decimal(by_name["Testolol 50"]["gst_rate"]) == Decimal("12.00")
    assert Decimal(by_name["Dettol Soap"]["gst_rate"]) == Decimal("18.00")
    # Two soaps out of a box of twelve priced at 240: 20 each.
    assert Decimal(by_name["Dettol Soap"]["gross_amount"]) == Decimal("40.00")


def test_the_printed_bill_knows_which_lines_are_general_items(client, admin_headers):
    """HSN and batch mean something for a medicine and nothing for a bar of
    soap — the print view needs each line's own item type to tell them
    apart, not just its name."""
    medicine = make_medicine(client, admin_headers, pack_size="10", hsn_code="3004")
    make_batch(client, admin_headers, medicine["id"], mrp_per_pack="100.00")

    soap = make_general(client, admin_headers, pack_size="12")
    make_batch(client, admin_headers, soap["id"], batch_no="SOAP-1", mrp_per_pack="240.00")

    sale = sell(
        client, admin_headers,
        [line_of(medicine["id"], "10", discount_pct="0"),
         line_of(soap["id"], "2", discount_pct="0")],
    )
    by_name = {item["medicine_name"]: item for item in sale["items"]}

    assert by_name["Testolol 50"]["item_type"] == "MEDICINE"
    assert by_name["Dettol Soap"]["item_type"] == "GENERAL"


def test_the_bill_shows_gst_for_each_line_and_totals_by_rate(client, admin_headers):
    """Different products, different slabs — so the tax has to be per line,
    and the summary has to split CGST from SGST."""
    medicine = make_medicine(client, admin_headers, pack_size="10")
    make_batch(client, admin_headers, medicine["id"], mrp_per_pack="112.00", gst_rate="12")

    soap = make_general(client, admin_headers, pack_size="1")
    make_batch(
        client, admin_headers, soap["id"], batch_no="SOAP-1",
        mrp_per_pack="118.00", gst_rate="18", expiry_date=iso(400),
    )

    sale = sell(
        client, admin_headers,
        [line_of(medicine["id"], "10", discount_pct="0"),
         line_of(soap["id"], "1", discount_pct="0")],
    )
    payload = client.get(f"{API}/sales/{sale['id']}/print", headers=admin_headers).json()

    # Every line carries its own tax, not just the rate.
    for item in payload["sale"]["items"]:
        assert Decimal(item["cgst_amount"]) + Decimal(item["sgst_amount"]) > 0

    rates = {row["gst_rate"]: row for row in payload["tax_summary"]}
    assert set(rates) == {"12.00", "18.00"}
    for row in rates.values():
        assert Decimal(row["cgst_amount"]) == Decimal(row["sgst_amount"])
        assert Decimal(row["total_tax"]) == Decimal(row["cgst_amount"]) * 2


@pytest.mark.parametrize("qty", ["1", "5", "12"])
def test_single_pieces_come_off_the_box(client, admin_headers, db, qty):
    soap = make_general(client, admin_headers, pack_size="12")
    batch = make_batch(client, admin_headers, soap["id"], qty_packs="1", mrp_per_pack="120.00")

    sell(client, admin_headers, [line_of(soap["id"], qty, discount_pct="0")])

    stored = db.get(Batch, batch["id"])
    assert stored.qty_available_units == Decimal("12.000") - Decimal(qty)


# --- the general-items screen --------------------------------------------


def test_the_master_list_can_be_scoped_to_one_kind(client, admin_headers):
    """Each maintenance screen lists only its own rows."""
    make_medicine(client, admin_headers, brand_name="Crocin 650")
    make_general(client, admin_headers, brand_name="Dettol Soap")

    general = client.get(
        f"{API}/medicines?item_type=GENERAL&page_size=100", headers=admin_headers
    ).json()
    assert [r["brand_name"] for r in general["items"]] == ["Dettol Soap"]
    # The row carries what the edit form needs, not just stock figures.
    row = general["items"][0]
    assert row["pack_size"] is not None
    assert row["pack_unit"] == "PIECE"


def test_the_counter_search_can_be_scoped_too(client, admin_headers):
    make_medicine(client, admin_headers, brand_name="Soapolol")
    make_general(client, admin_headers, brand_name="Soap Bar")

    both = client.get(f"{API}/medicines/search?q=soap", headers=admin_headers).json()
    only = client.get(
        f"{API}/medicines/search?q=soap&item_type=GENERAL", headers=admin_headers
    ).json()

    # The POS omits the filter — a basket mixes both.
    assert len(both) == 2
    assert [r["brand_name"] for r in only] == ["Soap Bar"]


def test_stock_on_hand_can_be_scoped_to_general_goods(client, admin_headers):
    medicine = make_medicine(client, admin_headers)
    make_batch(client, admin_headers, medicine["id"])
    soap = make_general(client, admin_headers)
    make_batch(client, admin_headers, soap["id"], batch_no="S1")

    rows = client.get(
        f"{API}/inventory/stock?item_type=GENERAL", headers=admin_headers
    ).json()
    assert {r["medicine_id"] for r in rows["items"]} == {soap["id"]}


def test_a_general_item_can_be_edited_without_acquiring_a_composition(
    client, admin_headers
):
    """The screen sends the master fields on every save; none of them may
    turn a general item back into something with a salt."""
    soap = make_general(client, admin_headers, pack_size="12")

    updated = client.patch(
        f"{API}/medicines/{soap['id']}",
        json={
            "brand_name": "Dettol Soap 125g",
            "pack_size": "6",
            "pack_unit": "PIECE",
            "composition_text": "PARACETAMOL (500MG)",
        },
        headers=admin_headers,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()

    assert body["composition_id"] is None
    assert body["units_per_pack"] == 6
    assert body["item_type"] == "GENERAL"


# --- each half of the shop sees only its own ------------------------------


def _stock_both(client, admin_headers):
    """A medicine and a general item, each with a batch."""
    medicine = make_medicine(client, admin_headers, brand_name="Crocin 650")
    make_batch(client, admin_headers, medicine["id"], qty_packs="1")
    soap = make_general(client, admin_headers, brand_name="Dettol Soap", pack_size="1")
    make_batch(client, admin_headers, soap["id"], batch_no="S1", qty_packs="1")
    return medicine, soap


def test_alerts_can_be_scoped_to_one_kind(client, admin_headers, db):
    from app.models.inventory import Batch

    medicine, soap = _stock_both(client, admin_headers)
    # Age both batches so each shows up as expired.
    for row in db.query(Batch).all():
        row.expiry_date = TODAY - timedelta(days=1)
    db.commit()

    everything = client.get(f"{API}/alerts/expired", headers=admin_headers).json()
    medicines = client.get(
        f"{API}/alerts/expired?item_type=MEDICINE", headers=admin_headers
    ).json()
    general = client.get(
        f"{API}/alerts/expired?item_type=GENERAL", headers=admin_headers
    ).json()

    assert len(everything) == 2
    assert [r["medicine_id"] for r in medicines] == [medicine["id"]]
    assert [r["medicine_id"] for r in general] == [soap["id"]]


def test_the_alert_summary_counts_only_the_scoped_kind(client, admin_headers):
    _stock_both(client, admin_headers)

    both = client.get(f"{API}/alerts/summary", headers=admin_headers).json()
    general = client.get(
        f"{API}/alerts/summary?item_type=GENERAL", headers=admin_headers
    ).json()

    assert both["low_stock"] + both["out_of_stock"] >= 2
    assert general["low_stock"] + general["out_of_stock"] == 1


def test_the_purchase_register_can_be_scoped_to_one_kind(client, admin_headers):
    from tests.conftest import make_supplier

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    soap = make_general(client, admin_headers)

    def buy(medicine_id, batch_no, **extra):
        line = {
            "medicine_id": medicine_id, "batch_no": batch_no,
            "qty_packs": "1", "cost_per_pack": "50", "mrp_per_pack": "100",
            "expiry_date": iso(400),
        }
        line.update(extra)
        response = client.post(
            f"{API}/purchases",
            json={
                "supplier_id": supplier["id"],
                "supplier_invoice_no": f"INV-{batch_no}",
                "supplier_invoice_date": iso(0),
                "items": [line],
            },
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    med_invoice = buy(medicine["id"], "M1")
    gen_invoice = buy(soap["id"], "G1", expiry_basis="NONE", expiry_date=None)

    medicines = client.get(
        f"{API}/purchases?item_type=MEDICINE", headers=admin_headers
    ).json()
    general = client.get(f"{API}/purchases?item_type=GENERAL", headers=admin_headers).json()

    assert med_invoice["id"] in {r["id"] for r in medicines["items"]}
    assert gen_invoice["id"] not in {r["id"] for r in medicines["items"]}
    assert [r["id"] for r in general["items"]] == [gen_invoice["id"]]


def test_the_expiry_register_can_be_scoped_to_one_kind(client, admin_headers, db):
    from app.models.inventory import Batch

    medicine, soap = _stock_both(client, admin_headers)
    for row in db.query(Batch).all():
        row.expiry_date = TODAY - timedelta(days=1)
    db.commit()
    client.post(f"{API}/expiry-register/scan", headers=admin_headers)

    general = client.get(
        f"{API}/expiry-register?item_type=GENERAL", headers=admin_headers
    ).json()
    assert [r["medicine_id"] for r in general["items"]] == [soap["id"]]


def test_stock_with_no_shelf_life_never_reaches_the_expiry_register(
    client, admin_headers
):
    """A comb cannot expire, whatever the sentinel date says."""
    comb = make_general(client, admin_headers, brand_name="Comb", pack_size="1")
    make_batch(
        client, admin_headers, comb["id"], expiry_basis="NONE", expiry_date=None,
        qty_packs="5",
    )
    client.post(f"{API}/expiry-register/scan", headers=admin_headers)

    rows = client.get(f"{API}/expiry-register", headers=admin_headers).json()
    assert comb["id"] not in {r["medicine_id"] for r in rows["items"]}
    assert client.get(f"{API}/alerts/expired", headers=admin_headers).json() == []


def test_one_supplier_invoice_carries_both_kinds(client, admin_headers, db):
    """A distributor bills medicines and general goods on one document, so
    goods inward has to accept them on one invoice."""
    from tests.conftest import make_supplier

    from app.models.inventory import Batch

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers, pack_size="10")
    soap = make_general(client, admin_headers, pack_size="12")

    response = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "MIXED-1",
            "supplier_invoice_date": iso(0),
            "items": [
                {
                    "medicine_id": medicine["id"], "batch_no": "M1",
                    "expiry_date": iso(400), "qty_packs": "2",
                    "cost_per_pack": "80", "mrp_per_pack": "100", "gst_rate": "12",
                },
                {
                    "medicine_id": soap["id"], "batch_no": "G1",
                    "expiry_basis": "BEST_BEFORE", "expiry_date": None,
                    "mfg_date": iso(-30), "best_before_months": 24,
                    "qty_packs": "1",
                    "cost_per_pack": "200", "mrp_per_pack": "240", "gst_rate": "18",
                },
            ],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    invoice = response.json()

    assert len(invoice["items"]) == 2
    # Each line kept its own tax slab and its own way of stating shelf life.
    rates = {Decimal(i["gst_rate"]) for i in invoice["items"]}
    assert rates == {Decimal("12.00"), Decimal("18.00")}

    batches = {b.batch_no: b for b in db.query(Batch).all()}
    assert batches["M1"].expiry_basis == "EXPIRY_DATE"
    assert batches["G1"].expiry_basis == "BEST_BEFORE"
    assert batches["G1"].qty_available_units == Decimal("12.000")


def test_a_mixed_invoice_shows_on_the_register_under_either_kind(client, admin_headers):
    """It is genuinely both, so it must not vanish from one of them."""
    from tests.conftest import make_supplier

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers)
    soap = make_general(client, admin_headers)

    created = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "MIXED-2",
            "supplier_invoice_date": iso(0),
            "items": [
                {"medicine_id": medicine["id"], "batch_no": "M2", "expiry_date": iso(400),
                 "qty_packs": "1", "cost_per_pack": "10", "mrp_per_pack": "20"},
                {"medicine_id": soap["id"], "batch_no": "G2", "expiry_basis": "NONE",
                 "expiry_date": None, "qty_packs": "1", "cost_per_pack": "10",
                 "mrp_per_pack": "20"},
            ],
        },
        headers=admin_headers,
    ).json()

    for kind in ("MEDICINE", "GENERAL"):
        rows = client.get(f"{API}/purchases?item_type={kind}", headers=admin_headers).json()
        assert created["id"] in {r["id"] for r in rows["items"]}, kind


# --- bulk-loading general goods ------------------------------------------


def test_a_general_template_can_be_downloaded(client, admin_headers):
    response = client.get(
        f"{API}/medicines/import-template?format=csv&layout=general", headers=admin_headers
    )
    assert response.status_code == 200
    text = response.text

    assert "brand_name" in text and "manufacturer" in text and "pack_unit" in text
    # Soap has no salt and no schedule; asking for them invites nonsense.
    assert "composition" not in text
    assert "schedule_type" not in text


def test_a_general_excel_template_can_be_downloaded(client, admin_headers):
    response = client.get(
        f"{API}/medicines/import-template?format=xlsx&layout=general", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


def test_general_items_can_be_bulk_loaded(client, admin_headers):
    raw = (
        "brand_name,manufacturer,pack_unit,pack_size,hsn_code\n"
        "Dettol Soap,Reckitt,PIECE,12,3401\n"
        "Floor Cleaner,Reckitt,ML,500,3402\n"
    ).encode()

    response = client.post(
        f"{API}/medicines/import?layout=general",
        files={"file": ("general.csv", raw, "text/csv")},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 2

    rows = client.get(
        f"{API}/medicines?item_type=GENERAL&page_size=50", headers=admin_headers
    ).json()["items"]
    by_name = {r["brand_name"]: r for r in rows}

    assert by_name["Dettol Soap"]["composition_id"] is None
    assert by_name["Dettol Soap"]["units_per_pack"] == 12   # countable
    assert by_name["Floor Cleaner"]["units_per_pack"] == 1  # one bottle
    assert all(r["item_type"] == "GENERAL" for r in rows)
    assert all(r["is_prescription_required"] is False for r in rows)


def test_a_general_file_does_not_need_a_composition_column(client, admin_headers):
    raw = b"brand_name,manufacturer\nComb,Generic Co\n"
    response = client.post(
        f"{API}/medicines/import?layout=general",
        files={"file": ("general.csv", raw, "text/csv")},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1


def test_a_medicine_file_still_needs_a_composition_column(client, admin_headers):
    raw = b"brand_name,manufacturer\nCrocin,GSK\n"
    response = client.post(
        f"{API}/medicines/import?layout=standard",
        files={"file": ("meds.csv", raw, "text/csv")},
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MISSING_COLUMNS"
