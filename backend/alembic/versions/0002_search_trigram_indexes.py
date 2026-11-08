"""trigram indexes for the substring searches

The POS search runs `search_text LIKE '%needle%'` on every keystroke. A btree
index cannot answer a leading wildcard, so on PostgreSQL that is a sequential
scan of the whole catalogue, per keystroke, per operator. It is unnoticeable
with the sample data and becomes the slowest thing in the app once a real
supplier catalogue has been bulk-uploaded.

`pg_trgm` indexes the three-character sequences in a string, which is exactly
what a substring match needs, and turns those scans into index lookups.

PostgreSQL only. SQLite has no trigram operator classes and its own LIKE is
already a scan; this revision is a deliberate no-op there, so dev and test on
SQLite are unaffected. That asymmetry is fine — it is an index, not a
constraint, so it changes only how fast the same answer comes back.

Revision ID: 0002
Revises: 0001
Created: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (index name, table, column) for every column matched with a leading
#: wildcard. Keep this list honest: an entry here that is not searched that
#: way is an index the database maintains on every write for nothing.
TRIGRAM_INDEXES = [
    # The POS and catalogue search. The hottest query in the app.
    ("ix_medicines_search_text_trgm", "medicines", "search_text"),
    # The composition picker and the same-salt alternatives popover.
    ("ix_compositions_search_key_trgm", "compositions", "search_key"),
]


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in TRIGRAM_INDEXES:
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {name} ON {table} USING gin ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    if not _is_postgres():
        return

    for name, _table, _column in TRIGRAM_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    # The extension is left in place. Dropping it would take any other
    # trigram index in the database with it, and it costs nothing idle.
