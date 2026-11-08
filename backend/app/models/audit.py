"""Append-only audit trail and the gap-free document number sequences."""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base
from app.db.types import UUIDPK
from app.models.enums import AuditAction, DocumentType, values
from app.models.mixins import PKMixin, TimestampMixin


class AuditLog(PKMixin, TimestampMixin, Base):
    """One row per mutation (CLAUDE.md rule 6). Never updated, never deleted.

    `store_id` and `user_id` are nullable so failed logins — where neither is
    known yet — can still be recorded.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_audit_log_store_id_created_at", "store_id", "created_at"),
        CheckConstraint(
            f"action IN ({', '.join(repr(v) for v in values(AuditAction))})", name="action_valid"
        ),
    )

    store_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("stores.id"))
    user_id: Mapped[UUID | None] = mapped_column(UUIDPK(), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    # Polymorphic — the id of whatever row the action touched, in any table.
    # No ForeignKey for that reason; the audit trail must outlive the row.
    entity_id: Mapped[UUID | None] = mapped_column(UUIDPK())
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.action} {self.entity_type}#{self.entity_id}>"


class DocumentSequence(PKMixin, TimestampMixin, Base):
    """Per-store, per-document-type counter.

    Invoice numbers must be 8-digit, numeric and gap-free, so they are issued
    from this row under a lock inside the sale transaction — never from
    MAX(invoice_no)+1, which races.
    """

    __tablename__ = "document_sequences"
    __table_args__ = (
        UniqueConstraint("store_id", "doc_type", name="uq_document_sequences_store_id_doc_type"),
        CheckConstraint(
            f"doc_type IN ({', '.join(repr(v) for v in values(DocumentType))})",
            name="doc_type_valid",
        ),
    )

    store_id: Mapped[UUID] = mapped_column(
        UUIDPK(), ForeignKey("stores.id"), nullable=False, index=True
    )
    doc_type: Mapped[str] = mapped_column(String(20), nullable=False)
    next_number: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
