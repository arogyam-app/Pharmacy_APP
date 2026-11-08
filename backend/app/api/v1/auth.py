"""Login, token refresh, logout, profile, password change."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import func

from app.core.config import settings
from app.core.cookies import (
    clear_session_cookie,
    clearing_headers,
    read_session_cookie,
    set_session_cookie,
)
from app.core.deps import CurrentUser, DbSession
from app.core.exceptions import AuthError, NotFoundError
from app.core.rate_limit import login_limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    verify_password,
)
from app.models.enums import AuditAction
from app.models.store import Store
from app.models.user import RefreshToken, User
from app.schemas.auth import (
    AccessTokenResponse,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.common import Message
from app.schemas.user import ChangePasswordRequest, UserOut
from app.services import access, audit
from app.utils.clock import as_utc

router = APIRouter()

GENERIC_LOGIN_FAILURE = "Email or password is incorrect."


def _issue_tokens(
    db, user: User, request: Request, *, remember: bool = False
) -> tuple[str, str, int]:
    access_token, expires_in = create_access_token(
        user_id=user.id, store_id=user.store_id, role=user.role
    )
    plaintext, token_hash, expires_at = create_refresh_token(
        days=settings.remember_me_days if remember else None
    )
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=(request.headers.get("user-agent") or "")[:255] or None,
        )
    )
    return access_token, plaintext, expires_in


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest, request: Request, response: Response, db: DbSession
) -> TokenResponse:
    email = payload.email.strip().lower()
    login_limiter.check(email)

    user = db.query(User).filter(func.lower(User.email) == email).one_or_none()

    # Same message and roughly the same work for "no such user" and "wrong
    # password", so the endpoint does not enumerate accounts.
    if user is None or not verify_password(payload.password, user.password_hash):
        audit.record(
            db,
            action=AuditAction.LOGIN,
            entity_type="User",
            entity_id=user.id if user else None,
            user=None,
            store_id=user.store_id if user else None,
            after={"email": email, "result": "FAILED"},
            request=request,
        )
        db.commit()
        raise AuthError(GENERIC_LOGIN_FAILURE)

    if not user.is_active:
        raise AuthError("This account has been disabled. Contact your administrator.")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.last_login_at = datetime.now(timezone.utc)
    access_token, refresh_plaintext, expires_in = _issue_tokens(
        db, user, request, remember=payload.remember_me
    )
    # The browser keeps the session here — httpOnly, so no script can read
    # it. The token is still in the body for non-browser clients; a browser
    # simply ignores it and never writes it to disk.
    set_session_cookie(response, refresh_plaintext, remember=payload.remember_me)

    audit.record(
        db,
        action=AuditAction.LOGIN,
        entity_type="User",
        entity_id=user.id,
        user=user,
        after={"email": user.email, "result": "SUCCESS"},
        request=request,
    )
    db.commit()
    db.refresh(user)
    login_limiter.reset(email)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_plaintext,
        expires_in=expires_in,
        user=UserOut.model_validate(user),
    )


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(
    payload: RefreshRequest, request: Request, response: Response, db: DbSession
) -> AccessTokenResponse:
    """Rotates the refresh token: the presented one is revoked immediately.

    The token comes from the httpOnly cookie for a browser, or from the body
    for an API client. The cookie is preferred: a browser that has one is not
    holding the token anywhere script can read.
    """
    presented = read_session_cookie(request) or payload.refresh_token
    if not presented:
        raise AuthError(
            "Session expired. Please sign in again.",
            code="TOKEN_EXPIRED",
            headers=clearing_headers(),
        )

    token_hash = hash_refresh_token(presented)
    stored = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).one_or_none()

    now = datetime.now(timezone.utc)
    if stored is None or stored.revoked_at is not None or _expired(stored.expires_at, now):
        # The dead cookie is cleared on the way out, so the browser stops
        # presenting it on every reload. It rides on the error, because a
        # raise discards the injected response.
        raise AuthError(
            "Session expired. Please sign in again.",
            code="TOKEN_EXPIRED",
            headers=clearing_headers(),
        )

    user = db.get(User, stored.user_id)
    if user is None or not user.is_active:
        raise AuthError(
            "This account is no longer active.",
            code="UNAUTHENTICATED",
            headers=clearing_headers(),
        )

    # Rotation keeps whatever lifetime the original had: refreshing must not
    # quietly upgrade a shared-counter session into a month-long one, nor
    # downgrade a remembered one on every reload.
    remembered = (as_utc(stored.expires_at) - as_utc(stored.created_at)).days > (
        settings.refresh_token_expire_days
    )

    stored.revoked_at = now
    access_token, refresh_plaintext, expires_in = _issue_tokens(
        db, user, request, remember=remembered
    )
    set_session_cookie(response, refresh_plaintext, remember=remembered)
    db.commit()

    return AccessTokenResponse(
        access_token=access_token, refresh_token=refresh_plaintext, expires_in=expires_in
    )


@router.post("/logout", response_model=Message)
def logout(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    user: CurrentUser,
    db: DbSession,
) -> Message:
    presented = read_session_cookie(request) or payload.refresh_token
    # Clear the cookie whatever happens next: a sign-out that leaves the
    # browser holding a session is not a sign-out.
    clear_session_cookie(response)

    if presented:
        token_hash = hash_refresh_token(presented)
        stored = (
            db.query(RefreshToken)
            .filter(RefreshToken.token_hash == token_hash, RefreshToken.user_id == user.id)
            .one_or_none()
        )
        if stored is not None and stored.revoked_at is None:
            stored.revoked_at = datetime.now(timezone.utc)

    audit.record(
        db, action=AuditAction.LOGOUT, entity_type="User", entity_id=user.id, user=user,
        request=request,
    )
    db.commit()
    return Message(message="Signed out.")


@router.get("/me", response_model=MeResponse)
def me(user: CurrentUser, db: DbSession) -> MeResponse:
    store = db.get(Store, user.store_id)
    if store is None:
        raise NotFoundError("Store not found.")
    return MeResponse(
        user=UserOut.model_validate(user),
        store=store,
        permissions=access.permissions_for_user(db, user),
    )


@router.post("/change-password", response_model=Message, status_code=status.HTTP_200_OK)
def change_password(
    payload: ChangePasswordRequest, request: Request, user: CurrentUser, db: DbSession
) -> Message:
    if not verify_password(payload.current_password, user.password_hash):
        raise AuthError("Current password is incorrect.", field="current_password")
    if payload.current_password == payload.new_password:
        raise AuthError(
            "The new password must be different from the current one.",
            code="VALIDATION_ERROR",
            field="new_password",
            status_code=400,
        )

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False

    # Changing a password invalidates every other session.
    now = datetime.now(timezone.utc)
    for token in db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
    ):
        token.revoked_at = now

    audit.record(
        db,
        action=AuditAction.UPDATE,
        entity_type="User",
        entity_id=user.id,
        user=user,
        after={"password_changed": True},
        request=request,
    )
    db.commit()
    return Message(message="Password updated. Please sign in again.")


def _expired(expires_at: datetime, now: datetime) -> bool:
    # SQLite hands back naive datetimes; treat them as UTC.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now
