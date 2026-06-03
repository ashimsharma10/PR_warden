"""drop the unused override table

The `override` table (and its ORM model) was scaffolded but never written to or
read. Removing the dead schema. Kept as a forward migration rather than editing
001 so already-migrated databases drop the table cleanly.

Revision ID: 003
Revises: 002
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("override")


def downgrade() -> None:
    op.create_table(
        "override",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "pr_check_id", sa.Integer, sa.ForeignKey("pr_check.id"), nullable=False
        ),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
