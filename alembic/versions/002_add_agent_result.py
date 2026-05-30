"""add agent_result to pr_check

Revision ID: 002
Revises: 001
Create Date: 2026-05-30 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Additive, nullable column — safe to deploy with the agent flag still off.
    op.add_column("pr_check", sa.Column("agent_result", sa.JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("pr_check", "agent_result")
