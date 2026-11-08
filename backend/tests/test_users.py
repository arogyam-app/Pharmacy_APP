"""User administration and the role boundary.

The role check must live on the route, not only in the UI (CLAUDE.md rule 4),
so every admin endpoint is probed with a sales-exec token.
"""

import pytest

from app.models.audit import AuditLog
from app.models.user import RefreshToken, User
from tests.conftest import API, auth_headers


def test_admin_creates_a_sales_executive_who_can_then_sign_in(client, admin_headers, db):
    response = client.post(
        f"{API}/users",
        json={"full_name": "Counter Staff", "email": "Counter@TestPharmacy.in", "role": "SALES_EXEC"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    body = response.json()

    # No password supplied -> the server issues a temporary one, once.
    assert body["temporary_password"]
    assert body["user"]["email"] == "counter@testpharmacy.in"  # normalised
    assert body["user"]["must_change_password"] is True

    assert auth_headers(client, "counter@testpharmacy.in", body["temporary_password"])


def test_created_user_with_explicit_password_still_must_change_it(client, admin_headers):
    body = client.post(
        f"{API}/users",
        json={
            "full_name": "Counter Two",
            "email": "counter2@testpharmacy.in",
            "role": "SALES_EXEC",
            "password": "Chosen@123",
        },
        headers=admin_headers,
    ).json()

    assert body["temporary_password"] is None
    assert body["user"]["must_change_password"] is True


def test_duplicate_email_is_rejected(client, admin_headers, sales):
    response = client.post(
        f"{API}/users",
        json={"full_name": "Clash", "email": sales.email.upper(), "role": "SALES_EXEC"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EMAIL_ALREADY_EXISTS"


def test_weak_password_is_rejected_at_creation(client, admin_headers):
    response = client.post(
        f"{API}/users",
        json={"full_name": "Weak", "email": "weak@testpharmacy.in", "password": "abc"},
        headers=admin_headers,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/users", None),
        ("post", "/users", {"full_name": "Sneaky", "email": "sneaky@testpharmacy.in"}),
        ("patch", "/users/1", {"full_name": "Renamed"}),
        ("post", "/users/1/reset-password", None),
        ("post", "/users/1/deactivate", None),
        ("patch", "/settings", {"max_discount_pct": "50"}),
        ("patch", "/store", {"name": "Hijacked"}),
    ],
)
def test_sales_executive_is_blocked_from_admin_routes(client, sales_headers, method, path, payload):
    response = client.request(method.upper(), f"{API}{path}", json=payload, headers=sales_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN_ROLE"


def test_user_list_is_scoped_to_the_callers_store(client, admin_headers, sales, db):
    from app.core.security import hash_password
    from app.models.store import Store, StoreSettings

    other = Store(name="Other Branch", state_code="19")
    db.add(other)
    db.flush()
    db.add(StoreSettings(store_id=other.id))
    db.add(
        User(
            store_id=other.id,
            full_name="Other Staff",
            email="other@testpharmacy.in",
            password_hash=hash_password("Other@1234"),
            role="SALES_EXEC",
        )
    )
    db.commit()

    emails = {u["email"] for u in client.get(f"{API}/users", headers=admin_headers).json()["items"]}
    assert "other@testpharmacy.in" not in emails


def test_user_search_matches_name_or_email(client, admin_headers, sales):
    by_name = client.get(f"{API}/users?q=Test Sales", headers=admin_headers).json()
    by_email = client.get(f"{API}/users?q=sales@", headers=admin_headers).json()

    assert by_name["total"] == 1
    assert by_email["items"][0]["email"] == sales.email


def test_reset_password_returns_a_temp_password_and_kills_sessions(client, admin_headers, sales, db):
    from tests.conftest import SALES_EMAIL, SALES_PASSWORD

    auth_headers(client, SALES_EMAIL, SALES_PASSWORD)  # creates a session

    body = client.post(f"{API}/users/{sales.id}/reset-password", headers=admin_headers).json()
    assert body["temporary_password"]

    assert client.post(
        f"{API}/auth/login", json={"email": SALES_EMAIL, "password": SALES_PASSWORD}
    ).status_code == 401
    assert auth_headers(client, SALES_EMAIL, body["temporary_password"])

    tokens = db.query(RefreshToken).filter(RefreshToken.user_id == sales.id).all()
    assert tokens and tokens[0].revoked_at is not None


def test_deactivating_a_user_revokes_their_session_immediately(client, admin_headers, sales, db):
    from tests.conftest import SALES_EMAIL, SALES_PASSWORD

    headers = auth_headers(client, SALES_EMAIL, SALES_PASSWORD)
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 200

    assert client.post(f"{API}/users/{sales.id}/deactivate", headers=admin_headers).status_code == 200

    # The still-valid access token must stop working the moment the account is off.
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 401


def test_admin_cannot_deactivate_themselves(client, admin_headers, admin):
    response = client.post(f"{API}/users/{admin.id}/deactivate", headers=admin_headers)

    assert response.status_code == 409
    assert "your own account" in response.json()["detail"]["message"]


def test_the_last_admin_cannot_be_demoted_or_removed(client, admin_headers, admin, db):
    second = client.post(
        f"{API}/users",
        json={"full_name": "Second Admin", "email": "admin2@testpharmacy.in", "role": "ADMIN"},
        headers=admin_headers,
    ).json()["user"]

    # Two admins: demoting one is fine.
    assert client.post(f"{API}/users/{second['id']}/deactivate", headers=admin_headers).status_code == 200

    # Back to one: the store must keep an administrator.
    response = client.patch(
        f"{API}/users/{admin['id'] if isinstance(admin, dict) else admin.id}",
        json={"role": "SALES_EXEC"},
        headers=admin_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "LAST_ADMIN"


def test_role_change_invalidates_the_existing_token(client, admin_headers, sales, db):
    from tests.conftest import SALES_EMAIL, SALES_PASSWORD

    headers = auth_headers(client, SALES_EMAIL, SALES_PASSWORD)
    client.patch(f"{API}/users/{sales.id}", json={"role": "ADMIN"}, headers=admin_headers)

    # The old token still claims SALES_EXEC — it must not be honoured.
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 401


def test_every_user_mutation_writes_an_audit_row(client, admin_headers, db):
    created = client.post(
        f"{API}/users",
        json={"full_name": "Audited", "email": "audited@testpharmacy.in"},
        headers=admin_headers,
    ).json()["user"]
    client.patch(f"{API}/users/{created['id']}", json={"phone": "9876543210"}, headers=admin_headers)

    rows = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == "User", AuditLog.entity_id == created["id"])
        .order_by(AuditLog.id)
        .all()
    )
    assert [r.action for r in rows] == ["CREATE", "UPDATE"]
    assert '"password_hash": "***"' not in (rows[0].after_json or "")  # not captured at all
    assert "Audited" in rows[0].after_json
    assert "9876543210" in rows[1].after_json
