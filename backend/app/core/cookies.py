"""The session cookie: where the refresh token lives in a browser.

**No password is ever stored here, or anywhere else a browser can reach.**
Passwords exist in exactly one form in this system — an argon2 hash in
`users.password_hash` — and there is no code path that can turn one back into
the text somebody typed. "Stay signed in" is done with a *session token*
instead, which is the thing that can be revoked, expired and rotated; a
remembered password can be none of those.

What the cookie carries is the refresh token, and it carries it with three
flags that matter:

* **httpOnly** — script cannot read it. This is the reason the token moved
  off `localStorage`, where any injected script (a compromised dependency, a
  bad browser extension, a reflected XSS) could have lifted a 7-day session
  and replayed it from anywhere.
* **Secure** — refuses to travel over plain http, so the token cannot be
  read off a café network. Configurable only because the dev setup is http;
  it must be on in production.
* **SameSite=Lax** — the browser withholds the cookie on cross-site POSTs,
  which is what stops another site silently driving the refresh endpoint.

Only `/auth/refresh` and `/auth/logout` read it, so the cookie is scoped to
that path and is not attached to the hundreds of ordinary API calls that have
no business seeing it.
"""

from fastapi import Request, Response

from app.core.config import settings

#: The cookie is offered back only on the endpoints that consume it.
COOKIE_PATH = f"{settings.api_v1_prefix}/auth"


def set_session_cookie(response: Response, token: str, *, remember: bool) -> None:
    """Persist the refresh token in the browser.

    Without `remember` this is a **session cookie** — no `max_age`, so it is
    gone when the browser closes. That is the right default for a shop
    counter that several people share; "remember me" is the deliberate
    exception, not the norm.
    """
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.remember_me_days * 24 * 60 * 60 if remember else None,
        path=COOKIE_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


def clear_session_cookie(response: Response) -> None:
    """Remove it on sign-out.

    The flags have to match the ones it was set with, or the browser treats
    it as a different cookie and quietly leaves the original in place.
    """
    response.delete_cookie(
        key=settings.cookie_name,
        path=COOKIE_PATH,
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


def read_session_cookie(request: Request) -> str | None:
    return request.cookies.get(settings.cookie_name)


def clearing_headers() -> dict[str, str]:
    """The `Set-Cookie` that removes the session, as a raw header.

    Needed because a `Response` injected into a route is discarded when an
    exception unwinds past it — so a 401 raised for a dead session would
    otherwise leave the browser still holding the cookie and presenting it on
    every single reload. Formatted by Starlette on a throwaway response so the
    attributes cannot drift from `clear_session_cookie`.
    """
    probe = Response()
    clear_session_cookie(probe)
    return {"set-cookie": probe.headers["set-cookie"]}
