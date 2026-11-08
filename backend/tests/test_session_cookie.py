"""The session cookie, and what it deliberately does not contain.

"Remember me" is a *session token* with a lifetime, never a stored password.
The first test in this file is the one that says so, and it is written to
fail loudly if anybody ever puts a credential on the wire.
"""

from app.core.config import settings
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, API


def sign_in(client, remember=False, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
    response = client.post(
        f"{API}/auth/login",
        json={"email": email, "password": password, "remember_me": remember},
    )
    assert response.status_code == 200, response.text
    return response


def cookie_header(response) -> str:
    return response.headers.get("set-cookie", "")


# --- no password goes anywhere -------------------------------------------


def test_the_cookie_never_carries_the_password(client, admin):
    response = sign_in(client, remember=True)

    raw = cookie_header(response)
    assert ADMIN_PASSWORD not in raw
    assert ADMIN_PASSWORD not in response.text
    # The cookie value is the refresh token, which is opaque and revocable —
    # unlike a password, which is none of those things.
    assert client.cookies.get(settings.cookie_name) == response.json()["refresh_token"]


def test_the_password_is_not_recoverable_from_the_stored_hash(client, admin, db):
    from app.models.user import User

    stored = db.query(User).filter(User.email == ADMIN_EMAIL).one()
    assert stored.password_hash != ADMIN_PASSWORD
    assert stored.password_hash.startswith("$argon2")


# --- the flags that make it secure ---------------------------------------


def test_the_session_cookie_is_httponly_and_scoped_to_auth(client, admin):
    raw = cookie_header(sign_in(client))

    assert settings.cookie_name in raw
    # Script cannot read it — the reason it is not in localStorage.
    assert "HttpOnly" in raw
    # Withheld on cross-site POSTs, which is what keeps CSRF off /auth/refresh.
    assert "SameSite=lax" in raw.replace("samesite", "SameSite")
    # Not attached to the hundreds of calls that have no business seeing it.
    assert f"Path={API}/auth" in raw


def test_without_remember_me_it_is_a_session_cookie(client, admin):
    raw = cookie_header(sign_in(client, remember=False))

    # No Max-Age and no Expires: the browser drops it when it closes, which
    # is what a shared counter machine needs.
    assert "Max-Age" not in raw
    assert "expires" not in raw.lower()


def test_remember_me_gives_the_cookie_and_the_token_a_lifetime(client, admin, db):
    from app.models.user import RefreshToken

    raw = cookie_header(sign_in(client, remember=True))
    assert f"Max-Age={settings.remember_me_days * 24 * 60 * 60}" in raw

    # The server-side token has to outlive the cookie, or the cookie would
    # come back to a session that had already expired.
    token = db.query(RefreshToken).order_by(RefreshToken.id.desc()).first()
    lifetime = (token.expires_at - token.created_at).days
    assert lifetime > settings.refresh_token_expire_days


# --- the cookie carries the session --------------------------------------


def test_refresh_works_from_the_cookie_alone(client, admin):
    sign_in(client, remember=True)

    # No body token at all: the browser presents the cookie by itself.
    response = client.post(f"{API}/auth/refresh", json={})
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]

    # And the rotated token replaces the old cookie.
    assert client.cookies.get(settings.cookie_name) == response.json()["refresh_token"]


def test_rotation_keeps_the_remembered_lifetime(client, admin):
    sign_in(client, remember=True)
    rotated = client.post(f"{API}/auth/refresh", json={})

    assert f"Max-Age={settings.remember_me_days * 24 * 60 * 60}" in cookie_header(rotated)


def test_rotation_does_not_upgrade_a_counter_session(client, admin):
    """A reload must not turn a browser-session login into a month-long one."""
    sign_in(client, remember=False)
    rotated = client.post(f"{API}/auth/refresh", json={})

    assert rotated.status_code == 200, rotated.text
    assert "Max-Age" not in cookie_header(rotated)


def test_a_used_refresh_token_is_dead(client, admin):
    sign_in(client, remember=True)
    first = client.post(f"{API}/auth/refresh", json={}).json()["refresh_token"]
    client.post(f"{API}/auth/refresh", json={})

    # Replay the superseded token as an API client would. The cookie is
    # cleared first because the route prefers it when both are present — a
    # browser is never trusted to pass a token the server already has.
    client.cookies.clear()
    replay = client.post(f"{API}/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "TOKEN_EXPIRED"


def test_the_cookie_wins_when_a_stale_body_token_is_also_sent(client, admin):
    """A live cookie is not defeated by a stale token in the body."""
    stale = sign_in(client, remember=True).json()["refresh_token"]
    client.post(f"{API}/auth/refresh", json={})      # rotates; `stale` is now dead

    response = client.post(f"{API}/auth/refresh", json={"refresh_token": stale})
    assert response.status_code == 200, response.text


def test_signing_out_clears_the_cookie_and_kills_the_session(client, admin):
    login = sign_in(client, remember=True)
    token = login.json()["access_token"]

    out = client.post(
        f"{API}/auth/logout", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert out.status_code == 200, out.text
    assert 'Max-Age=0' in cookie_header(out) or '=""' in cookie_header(out)
    assert not client.cookies.get(settings.cookie_name)

    assert client.post(f"{API}/auth/refresh", json={}).status_code == 401


def test_a_refresh_with_neither_cookie_nor_token_is_refused(client, admin):
    response = client.post(f"{API}/auth/refresh", json={})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "TOKEN_EXPIRED"


def test_an_unknown_cookie_is_cleared_rather_than_left_to_repeat(client, admin):
    client.cookies.set(settings.cookie_name, "not-a-real-token", path=f"{API}/auth")

    response = client.post(f"{API}/auth/refresh", json={})
    assert response.status_code == 401
    assert "Max-Age=0" in cookie_header(response) or '=""' in cookie_header(response)


# --- the body path still works for non-browser clients -------------------


def test_an_api_client_can_still_refresh_with_a_body_token(client, admin):
    token = sign_in(client).json()["refresh_token"]
    client.cookies.clear()

    response = client.post(f"{API}/auth/refresh", json={"refresh_token": token})
    assert response.status_code == 200, response.text
