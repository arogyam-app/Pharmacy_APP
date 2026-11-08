"""Gap-free document numbers.

Never `MAX(number) + 1`: two operators saving at the same moment would both
read the same maximum and issue the same number. The counter is a row that
is locked, read and incremented inside the caller's transaction, so a
concurrent request waits rather than duplicating.

`with_for_update()` renders `SELECT … FOR UPDATE` on PostgreSQL and is a
no-op on SQLite (whose writes are serialised anyway) — the code is written
to be correct on Postgres, per CLAUDE.md rule 7.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AppError
from app.models.audit import DocumentSequence
from app.models.enums import DocumentType

NUMBER_WIDTH = 8


def starting_number(doc_type: DocumentType) -> int:
    """The first number a series issues.

    Sale invoices open at `settings.invoice_number_start` so the shop's
    numbering carries on past whatever its paper book reached, rather than
    reusing numbers a customer may already hold. Every other series — returns,
    credit notes, purchases, adjustments — is internal and starts at 1.
    """
    if doc_type is DocumentType.SALE:
        return max(1, settings.invoice_number_start)
    return 1


def next_document_number(db: Session, store_id: UUID, doc_type: DocumentType) -> str:
    """Reserve the next number for a document type, zero-padded to 8 digits."""
    row = (
        db.query(DocumentSequence)
        .filter(
            DocumentSequence.store_id == store_id,
            DocumentSequence.doc_type == doc_type.value,
        )
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        # Seeded for every store; a missing row means the store was created
        # outside the seeder rather than that the number is simply unused.
        # It still has to open where the seeder would have opened it.
        row = DocumentSequence(
            store_id=store_id,
            doc_type=doc_type.value,
            next_number=starting_number(doc_type),
        )
        db.add(row)
        db.flush()

    number = row.next_number
    if number >= 10**NUMBER_WIDTH:
        raise AppError(
            f"The {doc_type.value.lower()} number sequence is exhausted.",
            code="SEQUENCE_EXHAUSTED",
            status_code=500,
        )

    row.next_number = number + 1
    db.flush()
    return str(number).zfill(NUMBER_WIDTH)
