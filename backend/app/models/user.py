"""Application users and their refresh tokens."""

from uuid import UUID

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import UUIDPK
from app.models.enums import UserRole, values
from app.models.mixins import ActorMixin, PKMixin, SoftDeleteMixin, TimestampMixin


class User(PKMixin, TimestampMixin, ActorMixin, SoftDeleteMixin, Base):
    """Admin or sales executive. Email is the login id and is globally unique
    so a person cannot be ambiguous once more stores exist."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint(
            f"role IN ({', '.join(repr(v) for v in values(UserRole))})", name="role_valid"
        ),
    )

    store_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("stores.id"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(15))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.SALES_EXEC.value)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Admin-created accounts start with a temporary password.
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.id} {self.email!r} {self.role}>"


class UserPermissionAccess(PKMixin, TimestampMixin, ActorMixin, Base):
    """One admin decision that departs from a user's role defaults.

    Only *exceptions* are stored. A user with no rows here holds exactly what
    the role policy says, so a later change to that policy still reaches
    everyone who was never given an exception — which is the whole reason
    this table records departures rather than a full permission list.

    `is_granted` carries both directions: True hands somebody a capability
    their role does not include, False takes one away. A revoke has to be a
    row rather than a deleted row, because "not mentioned" already means
    "whatever the role says".

    No CHECK constrains `permission` against the catalogue. Capabilities are
    added and renamed as the app grows, and a constraint listing them by name
    would need a migration every time — while an unknown key is already inert,
    because `resolve_permissions` ignores anything it does not recognise.
    """

    __tablename__ = "user_permission_access"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "permission", name="uq_user_permission_access_user_id_permission"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("users.id"), nullable=False, index=True
    )
    permission: Mapped[str] = mapped_column(String(60), nullable=False)
    is_granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Why an owner made the exception — worth having when someone asks six
    # months later why one executive can receive goods.
    note: Mapped[str | None] = mapped_column(String(200))

    def __repr__(self) -> str:  # pragma: no cover
        verb = "grant" if self.is_granted else "revoke"
        return f"<UserPermissionAccess {verb} {self.permission} user={self.user_id}>"


class RefreshToken(PKMixin, TimestampMixin, Base):
    """Only the SHA-256 of the token is stored, so a database leak cannot be
    replayed as a session."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),)

    user_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("users.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="refresh_tokens")
