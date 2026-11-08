"""User schemas. Password rules live here so every entry point shares them."""

from uuid import UUID

import re
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.common import ORMModel

MIN_PASSWORD_LENGTH = 8


def validate_password_strength(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if not re.search(r"[A-Za-z]", value):
        raise ValueError("Password must contain at least one letter.")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain at least one digit.")
    return value


class UserOut(ORMModel):
    id: UUID
    store_id: UUID
    full_name: str
    email: EmailStr
    phone: str | None = None
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    created_at: datetime


class UserCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=15)
    role: UserRole = UserRole.SALES_EXEC
    # Omit to have the server generate a temporary one the admin hands over.
    password: str | None = None

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str | None) -> str | None:
        return validate_password_strength(value) if value else value


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=15)
    role: UserRole | None = None
    is_active: bool | None = None


class UserCreated(BaseModel):
    user: UserOut
    # Present only when the server generated the password.
    temporary_password: str | None = None


class PasswordResetResult(BaseModel):
    user_id: UUID
    temporary_password: str


class PermissionOverride(BaseModel):
    permission: str
    is_granted: bool


class UserAccessOut(BaseModel):
    """One person's access: the role policy, the exceptions, and the sum."""

    user_id: UUID
    role: UserRole
    #: What this role holds with no exceptions at all.
    default_permissions: list[str]
    #: The effective set — this is what the gates enforce.
    permissions: list[str]
    overrides: list[PermissionOverride]
    #: Cannot be revoked however the admin ticks the boxes.
    locked_permissions: list[str]
    #: Cannot be granted to this role at all — cost visibility and anything
    #: that would let a non-admin rewrite the policy governing them.
    unavailable_permissions: list[str]


class UserAccessUpdate(BaseModel):
    """The full desired set, not a diff — see services/access.set_permissions."""

    permissions: list[str]
    note: str | None = Field(default=None, max_length=200)


class PermissionCatalogEntry(BaseModel):
    key: str
    label: str
    group: str
    group_label: str
    #: May an admin hand this to someone whose role does not include it?
    delegable: bool
    #: Never removable from an admin.
    locked: bool
    default_roles: list[str]


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        return validate_password_strength(value)
