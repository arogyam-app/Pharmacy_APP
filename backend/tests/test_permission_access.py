"""Per-user capabilities: the default policy, and the exceptions on top.

The first test is the policy itself, written out longhand. If somebody
changes what a sales executive can do, this file should be the thing that
tells them — not a shop discovering it at the counter.
"""

import pytest

from app.core.permissions import ALL_PERMISSIONS, default_permissions
from app.models.enums import UserRole
from tests.conftest import API, auth_headers, make_medicine, stocked_medicine

#: What a sales executive can do out of the box, per the shop's brief:
#: the counter in full, stock and alerts to look at, masters to add to,
#: customers and doctors to add and correct but never remove.
EXEC_DEFAULTS = {
    "pos:sell",
    "sales:read",
    "sales:reprint",
    "sales:request_void",
    "returns:create",
    "credit_notes:read",
    "inventory:read",
    "alerts:read",
    "expiry:read",
    "catalog:read",
    "medicines:create",
    "compositions:create",
    "manufacturers:create",
    "customers:read",
    "customers:create",
    "customers:edit",
    "doctors:read",
    "doctors:create",
    "doctors:edit",
}


def me(client, headers):
    response = client.get(f"{API}/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def access_of(client, headers, user_id):
    response = client.get(f"{API}/users/{user_id}/permissions", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def set_access(client, headers, user_id, permissions, note=None, expect=200):
    response = client.put(
        f"{API}/users/{user_id}/permissions",
        json={"permissions": sorted(permissions), "note": note},
        headers=headers,
    )
    assert response.status_code == expect, response.text
    return response.json()


def grant(client, admin_headers, user_id, *extra):
    return set_access(client, admin_headers, user_id, EXEC_DEFAULTS | set(extra))


def revoke(client, admin_headers, user_id, *dropped, note=None):
    return set_access(
        client, admin_headers, user_id, EXEC_DEFAULTS - set(dropped), note=note
    )


# --- the policy ----------------------------------------------------------


def test_a_sales_executive_starts_with_exactly_the_agreed_set(client, sales_headers, sales):
    assert set(me(client, sales_headers)["permissions"]) == EXEC_DEFAULTS
    assert set(default_permissions(UserRole.SALES_EXEC.value)) == EXEC_DEFAULTS


def test_an_admin_starts_with_everything(client, admin_headers):
    assert set(me(client, admin_headers)["permissions"]) == set(ALL_PERMISSIONS)


def test_a_fresh_user_has_no_stored_exceptions(client, admin_headers, sales):
    body = access_of(client, admin_headers, sales.id)
    assert body["overrides"] == []
    assert body["permissions"] == body["default_permissions"]


def test_the_catalogue_covers_every_capability_and_flags_the_undelegable(
    client, admin_headers
):
    catalogue = client.get(f"{API}/users/permissions/catalogue", headers=admin_headers).json()

    assert {c["key"] for c in catalogue} == set(ALL_PERMISSIONS)
    assert all(c["label"] and c["group_label"] for c in catalogue)

    undelegable = {c["key"] for c in catalogue if not c["delegable"]}
    # Cost visibility is rule 4; the rest would let a non-admin rewrite the
    # policy that governs them.
    assert "cost:view" in undelegable
    assert "reports:view" in undelegable
    assert "users:manage" in undelegable
    assert "settings:manage" in undelegable
    assert "purchases:receive" not in undelegable


# --- what the counter can do by default ----------------------------------


def test_the_counter_runs_end_to_end(client, admin_headers, sales_headers):
    medicine, _ = stocked_medicine(client, admin_headers, brand_name="Counterol")

    quote = client.post(
        f"{API}/pos/quote",
        json={"items": [{"medicine_id": medicine["id"], "qty_units": "1"}]},
        headers=sales_headers,
    )
    assert quote.status_code == 200, quote.text
    assert client.get(f"{API}/sales", headers=sales_headers).status_code == 200
    # And a return can be raised against what was just sold.
    assert client.get(
        f"{API}/credit-notes", headers=sales_headers
    ).status_code == 200


@pytest.mark.parametrize(
    "path", ["/inventory/stock", "/alerts/summary", "/expiry-register"]
)
def test_stock_screens_are_readable(client, sales_headers, path):
    assert client.get(f"{API}{path}", headers=sales_headers).status_code == 200


def test_but_stock_cannot_be_acted_on(client, admin_headers, sales_headers):
    medicine = make_medicine(client, admin_headers, brand_name="Lookonlyol")

    # Adding a batch is an inventory change, not a look.
    refused = client.post(
        f"{API}/inventory/batches",
        json={
            "medicine_id": medicine["id"], "batch_no": "B1",
            "expiry_date": "2030-01-01", "qty_packs": "1", "mrp_per_pack": "10.00",
            "gst_rate": "12",
        },
        headers=sales_headers,
    )
    assert refused.status_code == 403
    # Scanning the expiry register writes to it.
    assert client.post(f"{API}/expiry-register/scan", headers=sales_headers).status_code == 403


def test_masters_can_be_added_to_but_not_altered(client, admin_headers, sales_headers):
    created = client.post(
        f"{API}/medicines",
        json={
            "brand_name": "Addedbyexec", "manufacturer_name": "Exec Labs",
            "composition_text": "Paracetamol 500mg", "dosage_form": "TABLET",
            "pack_size": "10",
        },
        headers=sales_headers,
    )
    assert created.status_code == 201, created.text

    medicine_id = created.json()["id"]
    assert client.patch(
        f"{API}/medicines/{medicine_id}", json={"brand_name": "Renamed"},
        headers=sales_headers,
    ).status_code == 403
    assert client.delete(
        f"{API}/medicines/{medicine_id}", headers=sales_headers
    ).status_code == 403

    assert client.post(
        f"{API}/compositions", json={"composition_text": "Cetirizine 10mg"},
        headers=sales_headers,
    ).status_code == 201
    assert client.post(
        f"{API}/manufacturers", json={"name": "Exec Pharma"}, headers=sales_headers
    ).status_code == 201


def test_customers_and_doctors_can_be_added_and_corrected_but_not_removed(
    client, sales_headers
):
    customer = client.post(
        f"{API}/customers", json={"mobile": "9800001111", "name": "Walk In"},
        headers=sales_headers,
    )
    assert customer.status_code == 201, customer.text
    assert client.patch(
        f"{API}/customers/{customer.json()['id']}", json={"name": "Walk In Corrected"},
        headers=sales_headers,
    ).status_code == 200
    assert client.delete(
        f"{API}/customers/{customer.json()['id']}", headers=sales_headers
    ).status_code == 403

    doctor = client.post(
        f"{API}/doctors", json={"name": "Dr Counter"}, headers=sales_headers
    )
    assert doctor.status_code == 201, doctor.text
    assert client.patch(
        f"{API}/doctors/{doctor.json()['id']}", json={"name": "Dr Counter Jr"},
        headers=sales_headers,
    ).status_code == 200
    assert client.delete(
        f"{API}/doctors/{doctor.json()['id']}", headers=sales_headers
    ).status_code == 403


@pytest.mark.parametrize("path", ["/purchases", "/order-book", "/reports/gst/outward-summary"])
def test_the_owner_s_screens_are_shut_by_default(client, sales_headers, path):
    response = client.get(f"{API}{path}", headers=sales_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_ROLE"


# --- what an admin can hand over -----------------------------------------


def test_receive_goods_can_be_granted(client, admin_headers, sales_headers, sales, db):
    from tests.conftest import make_supplier

    supplier = make_supplier(client, admin_headers)
    medicine = make_medicine(client, admin_headers, brand_name="Inwardol")

    assert client.get(f"{API}/purchases", headers=sales_headers).status_code == 403
    grant(client, admin_headers, sales.id, "purchases:read", "purchases:receive")

    assert client.get(f"{API}/purchases", headers=sales_headers).status_code == 200
    booked = client.post(
        f"{API}/purchases",
        json={
            "supplier_id": supplier["id"],
            "supplier_invoice_no": "SUP-900",
            "supplier_invoice_date": "2026-08-01",
            "items": [{
                "medicine_id": medicine["id"], "batch_no": "PB-9",
                "expiry_date": "2030-01-01", "qty_packs": "5",
                "cost_per_pack": "100.00", "mrp_per_pack": "150.00", "gst_rate": "12",
            }],
        },
        headers=sales_headers,
    )
    assert booked.status_code == 201, booked.text
    # The work is delegated; the money is not (CLAUDE.md rule 4).
    assert "total_amount" not in booked.json()


def test_the_order_book_can_be_granted_in_full(client, admin_headers, sales_headers, sales):
    assert client.get(f"{API}/order-book", headers=sales_headers).status_code == 403

    grant(client, admin_headers, sales.id, "order_book:read", "order_book:manage")

    assert client.get(f"{API}/order-book", headers=sales_headers).status_code == 200
    assert client.post(f"{API}/order-book/recompute", headers=sales_headers).status_code == 200


def test_deleting_customers_and_doctors_can_be_granted(
    client, admin_headers, sales_headers, sales
):
    customer = client.post(
        f"{API}/customers", json={"mobile": "9800002222", "name": "Removable"},
        headers=sales_headers,
    ).json()
    doctor = client.post(
        f"{API}/doctors", json={"name": "Dr Removable"}, headers=sales_headers
    ).json()

    grant(client, admin_headers, sales.id, "customers:delete", "doctors:delete")

    assert client.delete(
        f"{API}/customers/{customer['id']}", headers=sales_headers
    ).status_code == 200
    assert client.delete(
        f"{API}/doctors/{doctor['id']}", headers=sales_headers
    ).status_code == 200


def test_cost_visibility_can_never_be_handed_over(client, admin_headers, sales):
    body = set_access(
        client, admin_headers, sales.id, EXEC_DEFAULTS | {"cost:view"}, expect=409
    )
    assert body["detail"]["code"] == "PERMISSION_NOT_DELEGABLE"


@pytest.mark.parametrize("permission", ["reports:view", "users:manage", "settings:manage"])
def test_the_owner_s_own_capabilities_are_not_delegable(
    client, admin_headers, sales, permission
):
    body = set_access(
        client, admin_headers, sales.id, EXEC_DEFAULTS | {permission}, expect=409
    )
    assert body["detail"]["code"] == "PERMISSION_NOT_DELEGABLE"


# --- switching a default off ---------------------------------------------


def test_a_default_can_be_switched_off_and_the_route_refuses(
    client, admin_headers, sales_headers, sales
):
    assert client.post(
        f"{API}/doctors", json={"name": "Dr Before"}, headers=sales_headers
    ).status_code == 201

    revoke(client, admin_headers, sales.id, "doctors:create")

    refused = client.post(
        f"{API}/doctors", json={"name": "Dr After"}, headers=sales_headers
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "PERMISSION_FORBIDDEN"
    assert "administrator" in refused.json()["detail"]["message"]
    assert "doctors:create" not in me(client, sales_headers)["permissions"]


def test_returning_someone_to_the_policy_clears_every_exception(
    client, admin_headers, sales, db
):
    from app.models.user import UserPermissionAccess

    set_access(client, admin_headers, sales.id, {"pos:sell"})
    assert db.query(UserPermissionAccess).filter(
        UserPermissionAccess.user_id == sales.id
    ).count() > 0

    body = set_access(client, admin_headers, sales.id, EXEC_DEFAULTS)

    assert body["overrides"] == []
    assert db.query(UserPermissionAccess).filter(
        UserPermissionAccess.user_id == sales.id
    ).count() == 0


def test_an_exception_survives_a_promotion(client, admin_headers, sales, db):
    revoke(client, admin_headers, sales.id, "alerts:read")

    promoted = client.patch(
        f"{API}/users/{sales.id}", json={"role": "ADMIN"}, headers=admin_headers
    )
    assert promoted.status_code == 200, promoted.text

    body = access_of(client, admin_headers, sales.id)
    assert "alerts:read" not in body["permissions"]
    assert "reports:view" in body["permissions"]      # gained with the role


# --- the safety rails ----------------------------------------------------


def test_nobody_edits_their_own_access(client, admin_headers, admin):
    body = set_access(client, admin_headers, admin.id, {"pos:sell"}, expect=409)
    assert body["detail"]["code"] == "CANNOT_EDIT_OWN_ACCESS"


def test_an_admin_cannot_be_shut_out_of_user_administration(client, admin_headers, admin, db):
    from app.core.security import hash_password
    from app.models.user import User

    other = User(
        store_id=admin.store_id, full_name="Second Admin", email="admin2@testpharmacy.in",
        password_hash=hash_password("Admin@1234"), role=UserRole.ADMIN.value,
    )
    db.add(other)
    db.commit()
    db.refresh(other)

    body = set_access(
        client, admin_headers, other.id,
        set(ALL_PERMISSIONS) - {"users:manage"}, expect=409,
    )
    assert body["detail"]["code"] == "PERMISSION_LOCKED"
    assert access_of(client, admin_headers, other.id)["locked_permissions"] == ["users:manage"]


def test_revoking_from_an_admin_works(client, admin_headers, admin, db):
    from app.core.security import hash_password
    from app.models.user import User

    other = User(
        store_id=admin.store_id, full_name="Junior Admin", email="admin3@testpharmacy.in",
        password_hash=hash_password("Admin@1234"), role=UserRole.ADMIN.value,
    )
    db.add(other)
    db.commit()
    db.refresh(other)
    headers = auth_headers(client, "admin3@testpharmacy.in", "Admin@1234")

    assert client.get(f"{API}/reports/audit", headers=headers).status_code == 200

    set_access(client, admin_headers, other.id, set(ALL_PERMISSIONS) - {"reports:view"})

    refused = client.get(f"{API}/reports/audit", headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "PERMISSION_FORBIDDEN"


def test_an_unknown_capability_is_rejected(client, admin_headers, sales):
    body = set_access(client, admin_headers, sales.id, {"teleporter:use"}, expect=400)
    assert body["detail"]["code"] == "UNKNOWN_PERMISSION"


def test_a_sales_executive_cannot_change_anyone_s_access(client, sales_headers, sales):
    response = client.put(
        f"{API}/users/{sales.id}/permissions", json={"permissions": []}, headers=sales_headers
    )
    assert response.status_code == 403


def test_the_change_is_audited_with_before_and_after(client, admin_headers, sales, db):
    import json

    from app.models.audit import AuditLog

    revoke(client, admin_headers, sales.id, "returns:create", note="Refunds go to the owner")

    entry = (
        db.query(AuditLog)
        .filter(
            AuditLog.entity_type == "UserPermissionAccess", AuditLog.entity_id == sales.id
        )
        .one()
    )
    assert entry.reason == "Refunds go to the owner"
    assert "returns:create" in json.loads(entry.before_json)["permissions"]
    assert "returns:create" not in json.loads(entry.after_json)["permissions"]
