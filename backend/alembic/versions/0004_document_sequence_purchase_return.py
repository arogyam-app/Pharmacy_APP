"""allow PURCHASE_RETURN as a document type

Revision 0003 added the purchase-return tables, and `DocumentType` grew a
`PURCHASE_RETURN` member so debit notes get their own gap-free series. The
CHECK on `document_sequences.doc_type` spells its values out, and autogenerate
does not compare CHECK constraints — so 0003 created the new tables and left
the old constraint exactly as it was.

The result: every test passed (they build the schema from model metadata,
which was right) and the first debit note against a *migrated* database failed
with `CHECK constraint failed: ck_document_sequences_doc_type_valid`.

That is the third time this shape of bug has reached a running database here.
`tests/test_migrations.py` now derives the list of enum-backed constraints
from the model metadata instead of a hand-kept list, so a constraint that is
never added to the guard is no longer a constraint the guard ignores.

Revision ID: 0004
Revises: 0003
Created: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Values as of this revision. Written out rather than imported: a migration
#: has to keep working when the enum it was written against moves on again.
DOC_TYPES = ("SALE", "RETURN", "CREDIT_NOTE", "PURCHASE", "PURCHASE_RETURN", "ADJUSTMENT")
DOC_TYPES_BEFORE = tuple(v for v in DOC_TYPES if v != "PURCHASE_RETURN")


def _rewrite(values: Sequence[str]) -> None:
    allowed = ", ".join(repr(v) for v in values)
    with op.batch_alter_table("document_sequences", schema=None) as batch_op:
        batch_op.drop_constraint("doc_type_valid", type_="check")
        batch_op.create_check_constraint("doc_type_valid", f"doc_type IN ({allowed})")


def upgrade() -> None:
    _rewrite(DOC_TYPES)


def downgrade() -> None:
    """The debit-note counter goes with the value that is about to be
    disallowed. Its numbers are not reused: the documents that carry them are
    removed by 0003's downgrade."""
    op.execute("DELETE FROM document_sequences WHERE doc_type = 'PURCHASE_RETURN'")
    _rewrite(DOC_TYPES_BEFORE)
