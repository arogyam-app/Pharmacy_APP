"""add item_type_snapshot to sale_items

A bill prints HSN and batch for a medicine, and neither for a bar of soap
-- but `sale_items` had no record of which of the two a line actually was,
only `medicines.item_type` today, which can move on after the sale. Added
nullable, backfilled from the medicine each line actually drew from, then
made NOT NULL so every row after this migration is guaranteed to have one.

Revision ID: 0006
Revises: 0005
Created: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sale_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("item_type_snapshot", sa.String(length=20), nullable=True)
        )

    op.execute(
        """
        UPDATE sale_items
        SET item_type_snapshot = medicines.item_type
        FROM medicines
        WHERE medicines.id = sale_items.medicine_id
        """
    )
    # Anything left over has no medicine to backfill from (should not
    # happen -- medicine_id is a not-null FK -- but a bill must still print
    # something rather than fail to load over a historical gap).
    op.execute(
        "UPDATE sale_items SET item_type_snapshot = 'MEDICINE' "
        "WHERE item_type_snapshot IS NULL"
    )

    with op.batch_alter_table("sale_items", schema=None) as batch_op:
        batch_op.alter_column("item_type_snapshot", nullable=False)
        batch_op.create_check_constraint(
            "sale_items_item_type_snapshot_valid",
            "item_type_snapshot IN ('MEDICINE', 'GENERAL')",
        )


def downgrade() -> None:
    with op.batch_alter_table("sale_items", schema=None) as batch_op:
        batch_op.drop_constraint("sale_items_item_type_snapshot_valid", type_="check")
        batch_op.drop_column("item_type_snapshot")
