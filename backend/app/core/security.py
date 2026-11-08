"""Password hashing and JWT issue/verify.

Access tokens are short-lived and stateless. Refresh tokens are opaque random
strings; only their SHA-256 is persisted, so a database leak cannot be
replayed as a session.
"""

from uuid import UUID

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.exceptions import AuthError

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


# --- passwords -----------------------------------------------------------


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        # Malformed/legacy hash — treat as a failed login, never a 500.
        return False


def needs_rehash(hashed: str) -> bool:
    return pwd_context.needs_update(hashed)


def generate_temp_password(length: int = 10) -> str:
    """Readable temporary password for admin-created accounts."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length)) + "@1"


# --- access tokens -------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(*, user_id: UUID, store_id: UUID, role: str) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    expire = _now() + expires_delta
    payload = {
        # A JWT is JSON, which has no UUID type — both ids go in as strings
        # and are parsed back by `deps.get_current_user`.
        "sub": str(user_id),
        "store_id": str(store_id),
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(_now().timestamp()),
        "exp": int(expire.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise AuthError("Session expired or invalid. Please sign in again.", code="TOKEN_EXPIRED") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise AuthError("Wrong token type.", code="TOKEN_EXPIRED")
    return payload


# --- refresh tokens ------------------------------------------------------


def create_refresh_token(*, days: int | None = None) -> tuple[str, str, datetime]:
    """Returns (plaintext, sha256_hash, expires_at).

    `days` lets "remember me" issue a longer-lived token. The default stays
    the short one, so a shared counter machine forgets by itself.
    """
    plaintext = secrets.token_urlsafe(48)
    expires_at = _now() + timedelta(days=days or settings.refresh_token_expire_days)
    return plaintext, hash_refresh_token(plaintext), expires_at


def hash_refresh_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
