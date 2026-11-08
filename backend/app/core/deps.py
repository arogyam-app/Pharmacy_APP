"""Shared FastAPI dependencies: DB session, current user, role gates.

Role checks live here and are attached to *every* protected route. The SPA
hiding a menu item is presentation, not security (CLAUDE.md rule 4).
"""

from uuid import UUID

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import AuthError, PermissionError_
from app.core.permissions import BY_KEY, resolve_permissions
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.enums import UserRole
from app.models.store import StoreSettings
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthError("Authentication required.", code="UNAUTHENTICATED")

    payload = decode_access_token(credentials.credentials)
    try:
        # The claim is a string — JSON has no UUID. A token carrying anything
        # that is not one is a forgery or a token from before the key change,
        # and either way is not a session.
        user_id = UUID(payload["sub"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise AuthError("Malformed token.", code="TOKEN_EXPIRED") from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthError("This account is no longer active.", code="UNAUTHENTICATED")

    # A role change must not wait for the old access token to expire.
    if payload.get("role") != user.role:
        raise AuthError("Permissions changed. Please sign in again.", code="TOKEN_EXPIRED")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.ADMIN.value:
        raise PermissionError_("This action is restricted to administrators.")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def require_permission(*permissions: str):
    """Route gate: the caller must hold **at least one** of these capabilities.

    This is the enforcement half of the access screen. Hiding a button is
    presentation; a capability an admin has switched off has to return 403
    from the route itself, or the switch means nothing (CLAUDE.md rule 4).

    Several capabilities are accepted because some endpoints genuinely serve
    more than one job: the counter searches the catalogue while somebody else
    maintains it, so those reads answer to either. Only the endpoints unique
    to one job carry one capability.

    The refusal distinguishes two cases, because they are two different
    conversations. A role that never held this keeps returning the
    long-standing `FORBIDDEN_ROLE`, so nothing an existing client relies on
    changes. Only something an admin **deliberately switched off** answers
    `PERMISSION_FORBIDDEN` — the one case where telling somebody to go and ask
    is useful advice.
    """
    wanted = set(permissions)

    def dependency(user: CurrentUser, db: DbSession) -> User:
        from app.services import access

        overrides = access.overrides_for(db, user.id)
        held = set(resolve_permissions(user.role, overrides))
        if wanted & held:
            return user

        names = ", ".join(
            sorted(BY_KEY[key].label for key in wanted if key in BY_KEY) or sorted(wanted)
        )
        if any(overrides.get(key) is False for key in wanted):
            raise PermissionError_(
                f"Your access to {names} has been turned off by an administrator.",
                code="PERMISSION_FORBIDDEN",
            )
        raise PermissionError_(f"Your role does not include: {names}.")

    return dependency


def require_admin_permission(*permissions: str):
    """`require_admin` and `require_permission` in one dependency.

    For routes that must stay admin-only whatever the ticks say — where the
    capability exists to let an admin switch it *off*, never to hand it out.
    """
    gate = require_permission(*permissions)

    def dependency(user: CurrentUser, db: DbSession) -> User:
        require_admin(user)
        return gate(user, db)

    return dependency


def get_store_settings(db: Session, store_id: UUID) -> StoreSettings:
    settings_row = (
        db.query(StoreSettings).filter(StoreSettings.store_id == store_id).one_or_none()
    )
    if settings_row is None:
        raise AuthError(
            "This store has no settings row. Run `python -m app.db.seed`.",
            code="STORE_NOT_CONFIGURED",
            status_code=500,
        )
    return settings_row


def current_settings(db: DbSession, user: CurrentUser) -> StoreSettings:
    return get_store_settings(db, user.store_id)


CurrentSettings = Annotated[StoreSettings, Depends(current_settings)]


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
