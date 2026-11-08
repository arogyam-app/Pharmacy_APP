"""Authentication payloads."""

from pydantic import BaseModel, EmailStr

from app.schemas.store import StoreOut
from app.schemas.user import UserOut


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    # Keeps the session alive across browser restarts by giving the session
    # cookie a lifetime. It does **not** store the password — nothing does.
    remember_me: bool = False


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class RefreshRequest(BaseModel):
    # Optional: a browser sends the httpOnly session cookie instead and never
    # sees the token. Kept in the body for non-browser API clients.
    refresh_token: str | None = None


class AccessTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class MeResponse(BaseModel):
    user: UserOut
    store: StoreOut
    #: Every capability this person holds — role policy with the admin's
    #: exceptions applied. The sidebar and every button filter on this,
    #: and the server enforces the same list, so a control the API would
    #: refuse is never offered.
    permissions: list[str]
