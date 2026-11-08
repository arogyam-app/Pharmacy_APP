"""Authentication: login, token rotation, logout, password change."""

from datetime import datetime, timedelta, timezone

from app.core.rate_limit import login_limiter
from app.models.audit import AuditLog
from app.models.user import RefreshToken, User
from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    API,
    SALES_EMAIL,
    SALES_PASSWORD,
    auth_headers,
    login,
)


def test_login_returns_tokens_and_profile(client, admin):
    body = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert body["user"]["email"] == ADMIN_EMAIL
    assert body["user"]["role"] == "ADMIN"
    assert "password_hash" not in body["user"]


def test_login_is_case_insensitive_on_email(client, admin):
    response = client.post(
        f"{API}/auth/login", json={"email": ADMIN_EMAIL.upper(), "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200


def test_login_records_last_login_and_audit(client, admin, db):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    db.expire_all()
    assert db.get(User, admin.id).last_login_at is not None
    entries = db.query(AuditLog).filter(AuditLog.action == "LOGIN").all()
    assert len(entries) == 1
    assert '"result": "SUCCESS"' in entries[0].after_json


def test_wrong_password_is_rejected_without_leaking_which_field(client, admin):
    response = client.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "nope1234"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_CREDENTIALS"
    assert "password" not in response.json()["detail"].get("field", "")


def test_unknown_email_gives_the_same_message_as_a_wrong_password(client, admin):
    unknown = client.post(
        f"{API}/auth/login", json={"email": "ghost@testpharmacy.in", "password": "whatever1"}
    )
    wrong = client.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "whatever1"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_failed_login_is_audited(client, admin, db):
    client.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong1234"})

    entry = db.query(AuditLog).filter(AuditLog.action == "LOGIN").one()
    assert '"result": "FAILED"' in entry.after_json


def test_disabled_user_cannot_log_in(client, sales, db):
    sales.is_active = False
    db.commit()

    response = client.post(
        f"{API}/auth/login", json={"email": SALES_EMAIL, "password": SALES_PASSWORD}
    )
    assert response.status_code == 401
    assert "disabled" in response.json()["detail"]["message"].lower()


def test_repeated_failures_are_rate_limited(client, admin):
    for _ in range(login_limiter.max_attempts):
        client.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong1234"})

    blocked = client.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong1234"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"


def test_me_returns_user_store_and_permissions(client, sales_headers):
    body = client.get(f"{API}/auth/me", headers=sales_headers).json()

    assert body["user"]["role"] == "SALES_EXEC"
    assert body["store"]["name"] == "Test Pharmacy"
    assert "pos:sell" in body["permissions"]
    # The permission that gates cost price must never reach a sales exec.
    assert "cost:view" not in body["permissions"]


def test_admin_permissions_include_cost_and_reports(client, admin_headers):
    body = client.get(f"{API}/auth/me", headers=admin_headers).json()

    assert {"cost:view", "reports:view", "users:manage"} <= set(body["permissions"])


def test_protected_route_requires_a_token(client, admin):
    response = client.get(f"{API}/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "UNAUTHENTICATED"


def test_garbage_token_is_rejected(client, admin):
    response = client.get(f"{API}/auth/me", headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "TOKEN_EXPIRED"


def test_refresh_rotates_the_token_and_revokes_the_old_one(client, admin, db):
    tokens = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    refreshed = client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    new_tokens = refreshed.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # The presented token is single-use. Signing in also set the session
    # cookie, and the route prefers it over anything in the body — so an API
    # client is simulated by dropping it first, or the live cookie would
    # answer the call instead of the replayed token.
    client.cookies.clear()
    replay = client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "TOKEN_EXPIRED"

    # The rotated one still works.
    assert client.get(
        f"{API}/auth/me", headers={"Authorization": f"Bearer {new_tokens['access_token']}"}
    ).status_code == 200


def test_expired_refresh_token_is_rejected(client, admin, db):
    tokens = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    stored = db.query(RefreshToken).one()
    stored.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    response = client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 401


def test_logout_revokes_the_refresh_token(client, admin, db):
    tokens = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert client.post(
        f"{API}/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
    ).status_code == 200

    assert db.query(RefreshToken).one().revoked_at is not None
    assert client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).status_code == 401


def test_change_password_requires_the_current_one(client, sales, sales_headers):
    response = client.post(
        f"{API}/auth/change-password",
        json={"current_password": "wrong1234", "new_password": "Brand@New1"},
        headers=sales_headers,
    )
    assert response.status_code == 401
    assert response.json()["detail"]["field"] == "current_password"


def test_change_password_updates_credentials_and_kills_sessions(client, sales, db):
    tokens = login(client, SALES_EMAIL, SALES_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = client.post(
        f"{API}/auth/change-password",
        json={"current_password": SALES_PASSWORD, "new_password": "Brand@New1"},
        headers=headers,
    )
    assert response.status_code == 200

    assert client.post(
        f"{API}/auth/login", json={"email": SALES_EMAIL, "password": SALES_PASSWORD}
    ).status_code == 401
    assert auth_headers(client, SALES_EMAIL, "Brand@New1")

    db.expire_all()
    assert db.get(User, sales.id).must_change_password is False
    assert all(t.revoked_at is not None for t in db.query(RefreshToken).all()[:1])


def test_weak_new_password_is_rejected(client, sales_headers):
    response = client.post(
        f"{API}/auth/change-password",
        json={"current_password": SALES_PASSWORD, "new_password": "short"},
        headers=sales_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_ERROR"


def test_new_password_must_differ(client, sales_headers):
    response = client.post(
        f"{API}/auth/change-password",
        json={"current_password": SALES_PASSWORD, "new_password": SALES_PASSWORD},
        headers=sales_headers,
    )
    assert response.status_code == 400
