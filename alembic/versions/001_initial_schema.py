"""initial schema

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repo",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("installation_id", sa.Integer, unique=True, nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("cached_config", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "pr_check",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("repo_id", sa.Integer, sa.ForeignKey("repo.id"), nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("sha", sa.String(40), nullable=False),
        sa.Column("check_results", sa.JSON, nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("cost_usd", sa.Float, nullable=True),
        sa.Column("action_taken", sa.String(50), nullable=True),
        sa.Column("comment_id", sa.BigInteger, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_pr_check_repo_pr", "pr_check", ["repo_id", "pr_number"])

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


def downgrade() -> None:
    op.drop_table("override")
    op.drop_index("ix_pr_check_repo_pr", table_name="pr_check")
    op.drop_table("pr_check")
    op.drop_table("repo")
