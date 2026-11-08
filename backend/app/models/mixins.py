"""Column mixins shared by every table (see docs/DATA_MODEL.md §Conventions)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, declared_attr

from app.db.types import UUIDPK, uuid7


class PKMixin:
    """A UUID key, generated in Python, for every table.

    `default=` rather than a server default: the services flush and then read
    `row.id` to build the next row — a ledger line needs the batch it belongs
    to — and a database-side default is not visible until after the INSERT
    returns. Generating here means the id exists the moment the object does.
    """

    id: Mapped[UUID] = mapped_column(UUIDPK(), primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ActorMixin:
    """Who created / last touched the row."""

    @declared_attr
    def created_by_id(cls) -> Mapped[UUID | None]:
        return mapped_column(UUIDPK(), ForeignKey("users.id"), nullable=True)

    @declared_attr
    def updated_by_id(cls) -> Mapped[UUID | None]:
        return mapped_column(UUIDPK(), ForeignKey("users.id"), nullable=True)


class StoreScopedMixin:
    """The multi-store seam: every business row belongs to exactly one store."""

    @declared_attr
    def store_id(cls) -> Mapped[UUID]:
        return mapped_column(
            UUIDPK(), ForeignKey("stores.id"), nullable=False, index=True
        )


class SoftDeleteMixin:
    """Masters only — transactional rows are never deactivated or deleted."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BaseEntity(PKMixin, TimestampMixin, ActorMixin):
    """Standard non-tenant table."""


class StoreEntity(PKMixin, StoreScopedMixin, TimestampMixin, ActorMixin):
    """Standard store-scoped table."""
