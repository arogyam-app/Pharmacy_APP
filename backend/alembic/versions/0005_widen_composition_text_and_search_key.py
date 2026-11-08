"""widen composition_text and search_key from 400 to 1000 chars

A multi-strain probiotic or an amino-acid blend can list a dozen-plus
ingredients and clear 400 characters comfortably -- a real bulk import hit
`StringDataRightTruncation` on a 10-ingredient formulation. 1000 covers the
longest real formulation seen so far (354 chars) with headroom to spare.

Revision ID: 0005
Revises: 0004
Created: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("compositions", schema=None) as batch_op:
        batch_op.alter_column(
            "composition_text",
            existing_type=sa.String(length=400),
            type_=sa.String(length=1000),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "search_key",
            existing_type=sa.String(length=400),
            type_=sa.String(length=1000),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("compositions", schema=None) as batch_op:
        batch_op.alter_column(
            "search_key",
            existing_type=sa.String(length=1000),
            type_=sa.String(length=400),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "composition_text",
            existing_type=sa.String(length=1000),
            type_=sa.String(length=400),
            existing_nullable=False,
        )
