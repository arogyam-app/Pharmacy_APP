"""Spending a credit note on a later sale.

The phase's acceptance criterion: a credit note's balance is consumed
**exactly once**. Over-redemption, double redemption and redemption by the
wrong customer all have to be impossible.
"""

from app.utils.clock import business_date
from datetime import date, timedelta
from decimal import Decimal

from tests.conftest import API, sell, stocked_medicine

MOBILE = "9876500777"


def line_of(medicine_id, qty="1", **kw):
    return {"medicine_id": medicine_id, "qty_units": qty, **kw}


def note_worth(client, headers, amount_units="10", mobile=MOBILE, brand="Creditol"):
    """Sell then return, leaving a credit note on the customer's file."""
    medicine, batch = stocked_medicine(
        client, headers, mrp="100.00", qty_packs="50", brand_name=brand
    )
    sale = sell(
        client, headers, [line_of(medicine["id"], amount_units, discount_pct="0")],
        customer={"mobile": mobile, "name": "Credit Tester"},
    )
    returned = client.post(
        f"{API}/returns",
        json={
            "sale_id": sale["id"],
            "items": [{"sale_item_id": sale["items"][0]["id"], "qty_units": amount_units}],
        },
        headers=headers,
    ).json()
    note = client.get(
        f"{API}/credit-notes/{returned['credit_note_id']}", headers=headers
    ).json()
    return medicine, note


def pay_with_note(client, headers, medicine_id, qty, note, amount, cash=None):
    """Bill a sale settling `amount` from the note, the rest in cash."""
    payments = [{"mode": "CREDIT_NOTE", "amount": amount, "credit_note_id": note["id"]}]
    if cash:
        payments.append({"mode": "CASH", "amount": cash})
    return client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine_id, qty, discount_pct="0")],
            "customer": {"mobile": MOBILE},
            "payments": payments,
        },
        headers=headers,
    )


# --- redemption -----------------------------------------------------------


def test_a_credit_note_settles_a_later_invoice(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "10")
    assert note["balance_amount"] == "100.00"

    # A ₹60 sale paid entirely from the note.
    response = pay_with_note(client, admin_headers, medicine["id"], "6", note, "60.00")
    assert response.status_code == 201, response.text
    sale = response.json()

    assert sale["paid_amount"] == "60.00"
    payment = sale["payments"][0]
    assert payment["mode"] == "CREDIT_NOTE"
    assert payment["credit_note_id"] == note["id"]

    after = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
    assert after["amount_used"] == "60.00"
    assert after["balance_amount"] == "40.00"
    assert after["status"] == "PARTIALLY_USED"


def test_the_balance_is_consumed_exactly_once(client, admin_headers, db):
    """The roadmap's Phase 5 criterion."""
    from app.models.returns import CreditNoteUsage

    medicine, note = note_worth(client, admin_headers, "10")

    first = pay_with_note(client, admin_headers, medicine["id"], "10", note, "100.00")
    assert first.status_code == 201

    spent = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
    assert spent["balance_amount"] == "0.00"
    assert spent["status"] == "FULLY_USED"

    # A second attempt on the same note gets nothing.
    second = pay_with_note(client, admin_headers, medicine["id"], "10", note, "100.00")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "CREDIT_NOTE_INSUFFICIENT_BALANCE"

    usages = db.query(CreditNoteUsage).filter(
        CreditNoteUsage.credit_note_id == note["id"]
    ).all()
    assert len(usages) == 1
    assert usages[0].amount_used == Decimal("100.00")


def test_over_redeeming_in_one_go_is_refused(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "5")     # ₹50 on file

    response = pay_with_note(client, admin_headers, medicine["id"], "10", note, "100.00")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_INSUFFICIENT_BALANCE"

    # And nothing was taken from it.
    after = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
    assert after["balance_amount"] == "50.00"


def test_a_refused_redemption_bills_no_invoice_and_moves_no_stock(
    client, admin_headers, db
):
    """The whole confirm is one transaction."""
    from app.models.inventory import Batch
    from app.models.sale import Sale

    medicine, note = note_worth(client, admin_headers, "5")
    before_sales = db.query(Sale).count()
    batch_id = client.get(
        f"{API}/inventory/batches", params={"medicine_id": medicine["id"]},
        headers=admin_headers,
    ).json()["items"][0]["id"]
    before_stock = db.get(Batch, batch_id).qty_available_units

    pay_with_note(client, admin_headers, medicine["id"], "10", note, "100.00")

    db.expire_all()
    assert db.query(Sale).count() == before_sales
    assert db.get(Batch, batch_id).qty_available_units == before_stock


def test_a_note_can_be_split_across_two_visits(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "10")     # ₹100

    assert pay_with_note(
        client, admin_headers, medicine["id"], "3", note, "30.00"
    ).status_code == 201
    assert pay_with_note(
        client, admin_headers, medicine["id"], "4", note, "40.00"
    ).status_code == 201

    after = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
    assert after["amount_used"] == "70.00"
    assert after["balance_amount"] == "30.00"
    assert len(after["usages"]) == 2


def test_a_note_can_be_combined_with_cash(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "5")      # ₹50

    response = pay_with_note(
        client, admin_headers, medicine["id"], "8", note, "50.00", cash="30.00"
    )
    assert response.status_code == 201
    sale = response.json()
    assert sale["paid_amount"] == "80.00"
    assert {p["mode"] for p in sale["payments"]} == {"CREDIT_NOTE", "CASH"}


def test_the_usages_add_up_to_the_amount_used(client, admin_headers, db):
    """The balance is a cached figure; the usages are the truth."""
    from sqlalchemy import func

    from app.models.returns import CreditNote, CreditNoteUsage

    medicine, note = note_worth(client, admin_headers, "10")
    pay_with_note(client, admin_headers, medicine["id"], "3", note, "30.00")
    pay_with_note(client, admin_headers, medicine["id"], "2", note, "20.00")

    db.expire_all()
    row = db.get(CreditNote, note["id"])
    total = (
        db.query(func.sum(CreditNoteUsage.amount_used))
        .filter(CreditNoteUsage.credit_note_id == row.id)
        .scalar()
    )
    assert row.amount_used == total
    assert row.balance_amount == row.amount_issued - row.amount_used


def test_a_note_larger_than_the_bill_leaves_the_rest_on_account(client, admin_headers):
    """The customer pays nothing and keeps the remainder for next time."""
    medicine, note = note_worth(client, admin_headers, "10")      # ₹100 on file

    # A ₹40 sale, settled entirely from credit.
    response = pay_with_note(client, admin_headers, medicine["id"], "4", note, "40.00")
    assert response.status_code == 201
    sale = response.json()

    assert sale["net_amount"] == "40.00"
    assert sale["paid_amount"] == "40.00"
    assert len(sale["payments"]) == 1               # nothing was collected
    assert sale["payments"][0]["mode"] == "CREDIT_NOTE"

    after = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
    assert after["balance_amount"] == "60.00"       # kept for another visit
    assert after["status"] == "PARTIALLY_USED"


def test_a_bill_larger_than_the_note_leaves_the_rest_to_pay(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "5")       # ₹50 on file

    # A ₹90 sale: ₹50 from credit, ₹40 in cash.
    response = pay_with_note(
        client, admin_headers, medicine["id"], "9", note, "50.00", cash="40.00"
    )
    assert response.status_code == 201
    sale = response.json()

    assert sale["net_amount"] == "90.00"
    by_mode = {p["mode"]: p["amount"] for p in sale["payments"]}
    assert by_mode == {"CREDIT_NOTE": "50.00", "CASH": "40.00"}

    after = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
    assert after["balance_amount"] == "0.00"
    assert after["status"] == "FULLY_USED"


def test_one_note_spent_across_three_visits(client, admin_headers):
    """A balance is not use-it-or-lose-it; it lasts until it runs out."""
    medicine, note = note_worth(client, admin_headers, "10")      # ₹100

    for qty, spend in (("2", "20.00"), ("3", "30.00"), ("5", "50.00")):
        assert pay_with_note(
            client, admin_headers, medicine["id"], qty, note, spend
        ).status_code == 201

    after = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
    assert after["amount_used"] == "100.00"
    assert after["balance_amount"] == "0.00"
    assert after["status"] == "FULLY_USED"
    assert len(after["usages"]) == 3


def test_two_notes_can_settle_one_invoice(client, admin_headers):
    """A customer with several notes spends them together."""
    medicine, first = note_worth(client, admin_headers, "5", brand="Creditol A")
    _, second = note_worth(client, admin_headers, "5", brand="Creditol B")

    response = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "10", discount_pct="0")],   # ₹100
            "customer": {"mobile": MOBILE},
            "payments": [
                {"mode": "CREDIT_NOTE", "amount": "50.00", "credit_note_id": first["id"]},
                {"mode": "CREDIT_NOTE", "amount": "50.00", "credit_note_id": second["id"]},
            ],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert len(response.json()["payments"]) == 2

    for note in (first, second):
        after = client.get(f"{API}/credit-notes/{note['id']}", headers=admin_headers).json()
        assert after["balance_amount"] == "0.00"
        assert after["status"] == "FULLY_USED"


# --- who may spend it -----------------------------------------------------


def test_another_customers_note_cannot_be_spent(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "5")

    response = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "5", discount_pct="0")],
            "customer": {"mobile": "9811100000", "name": "Someone Else"},
            "payments": [
                {"mode": "CREDIT_NOTE", "amount": "50.00", "credit_note_id": note["id"]}
            ],
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_WRONG_CUSTOMER"


def test_a_walk_in_cannot_spend_a_note(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "5")

    response = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "5", discount_pct="0")],
            "payments": [
                {"mode": "CREDIT_NOTE", "amount": "50.00", "credit_note_id": note["id"]}
            ],
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_WRONG_CUSTOMER"


def test_paying_by_note_without_naming_one_is_refused(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers)

    response = client.post(
        f"{API}/sales",
        json={
            "items": [line_of(medicine["id"], "10", discount_pct="0")],
            "customer": {"mobile": MOBILE},
            "payments": [{"mode": "CREDIT_NOTE", "amount": "100.00"}],
        },
        headers=admin_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_REQUIRED"


# --- expiry and cancellation ---------------------------------------------


def test_an_expired_note_cannot_be_spent(client, admin_headers, db):
    from app.models.returns import CreditNote

    medicine, note = note_worth(client, admin_headers, "5")

    row = db.get(CreditNote, note["id"])
    row.expires_on = business_date() - timedelta(days=1)
    db.commit()

    response = pay_with_note(client, admin_headers, medicine["id"], "5", note, "50.00")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_EXPIRED"


def test_a_lapsed_note_stops_being_offered(client, admin_headers, db):
    from app.models.returns import CreditNote

    _, note = note_worth(client, admin_headers, "5")

    row = db.get(CreditNote, note["id"])
    row.expires_on = business_date() - timedelta(days=1)
    db.commit()

    listing = client.get(
        f"{API}/credit-notes", params={"open_only": True}, headers=admin_headers
    ).json()
    assert listing["total"] == 0

    db.expire_all()
    assert db.get(CreditNote, note["id"]).status == "EXPIRED"


def test_an_admin_can_cancel_an_unspent_note(client, admin_headers):
    _, note = note_worth(client, admin_headers, "5")

    response = client.post(
        f"{API}/credit-notes/{note['id']}/cancel",
        json={"reason": "Issued against the wrong invoice"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["balance_amount"] == "0.00"


def test_a_partly_spent_note_cannot_be_cancelled(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "10")
    pay_with_note(client, admin_headers, medicine["id"], "3", note, "30.00")

    response = client.post(
        f"{API}/credit-notes/{note['id']}/cancel",
        json={"reason": "Changed my mind"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CREDIT_NOTE_IN_USE"


def test_cancelling_is_admin_only(client, admin_headers, sales_headers):
    _, note = note_worth(client, admin_headers, "5")

    response = client.post(
        f"{API}/credit-notes/{note['id']}/cancel",
        json={"reason": "Not my call to make"},
        headers=sales_headers,
    )
    assert response.status_code == 403


def test_the_counter_can_see_a_customers_notes(client, admin_headers, sales_headers):
    _, note = note_worth(client, admin_headers, "5")

    listing = client.get(
        f"{API}/credit-notes", params={"mobile": MOBILE, "open_only": True},
        headers=sales_headers,
    ).json()
    assert listing["total"] == 1
    assert listing["items"][0]["balance_amount"] == "50.00"
    assert listing["items"][0]["customer_mobile"] == MOBILE


def test_the_customer_balance_follows_the_notes(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "10")

    before = client.get(f"{API}/customers/by-mobile/{MOBILE}", headers=admin_headers).json()
    assert before["credit_balance"] == "100.00"

    pay_with_note(client, admin_headers, medicine["id"], "4", note, "40.00")

    after = client.get(f"{API}/customers/by-mobile/{MOBILE}", headers=admin_headers).json()
    assert after["credit_balance"] == "60.00"


# --- what the bill shows --------------------------------------------------


def printed(client, headers, sale_id):
    response = client.get(f"{API}/sales/{sale_id}/print", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_bill_shows_what_is_left_to_pay_after_the_note(client, admin_headers):
    """The invoice still stands for its full value; the customer owes less."""
    medicine, note = note_worth(client, admin_headers, "10", brand="Payableol")
    response = pay_with_note(client, admin_headers, medicine["id"], "10", note, "60.00",
                             cash="40.00")
    assert response.status_code == 201, response.text
    sale = response.json()

    bill = printed(client, admin_headers, sale["id"])
    assert bill["credit_note_applied"] == "60.00"
    assert bill["amount_payable"] == "40.00"
    assert Decimal(bill["sale"]["net_amount"]) == Decimal("100.00")   # unchanged


def test_a_bill_settled_entirely_by_credit_asks_for_nothing(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "10", brand="Fullysettled")
    sale = pay_with_note(client, admin_headers, medicine["id"], "6", note, "60.00").json()

    bill = printed(client, admin_headers, sale["id"])
    assert bill["amount_payable"] == "0.00"
    assert bill["amount_payable_in_words"].lower().startswith("rupees zero")


def test_a_bill_with_no_credit_note_is_payable_in_full(client, admin_headers):
    medicine, _ = stocked_medicine(client, admin_headers, mrp="100.00", brand_name="Nocredit")
    sale = sell(
        client, admin_headers,
        [line_of(medicine["id"], "5", discount_pct="0")],
    )
    bill = printed(client, admin_headers, sale["id"])

    assert bill["credit_note_applied"] == "0.00"
    assert bill["amount_payable"] == sale["net_amount"]
    assert bill["amount_payable_in_words"] == bill["amount_in_words"]


def test_the_bill_names_the_note_and_what_is_left_on_it(client, admin_headers):
    """A customer needs the note's number and its remaining balance."""
    medicine, note = note_worth(client, admin_headers, "10", brand="Namedon")
    sale = pay_with_note(client, admin_headers, medicine["id"], "6", note, "60.00").json()

    payment = printed(client, admin_headers, sale["id"])["sale"]["payments"][0]
    assert payment["mode"] == "CREDIT_NOTE"
    assert payment["credit_note_no"] == note["credit_note_no"]
    assert payment["credit_note_balance"] == "40.00"


def test_the_words_match_the_figure_printed_above_them(client, admin_headers):
    medicine, note = note_worth(client, admin_headers, "10", brand="Wordsmatch")
    sale = pay_with_note(client, admin_headers, medicine["id"], "10", note, "60.00",
                         cash="40.00").json()

    bill = printed(client, admin_headers, sale["id"])
    assert bill["amount_payable_in_words"] == "Rupees forty only"
    assert bill["amount_in_words"] == "Rupees one hundred only"


def test_the_invoice_and_the_payments_still_reconcile(client, admin_headers):
    """Credit + cash must equal the invoice — the note is a payment."""
    medicine, note = note_worth(client, admin_headers, "10", brand="Reconciles")
    sale = pay_with_note(client, admin_headers, medicine["id"], "10", note, "60.00",
                         cash="40.00").json()

    bill = printed(client, admin_headers, sale["id"])
    paid = sum(Decimal(p["amount"]) for p in bill["sale"]["payments"])
    assert paid == Decimal(bill["sale"]["net_amount"])
    assert Decimal(bill["credit_note_applied"]) + Decimal(bill["amount_payable"]) == paid
