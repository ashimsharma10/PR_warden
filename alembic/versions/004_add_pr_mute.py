"""add pr_mute table for /warden mute

Backs the per-PR mute switch toggled by the `/warden mute` / `/warden unmute`
slash commands. A muted PR is skipped entirely by the review pipeline.

Revision ID: 004
Revises: 003
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pr_mute",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("repo_id", sa.Integer, sa.ForeignKey("repo.id"), nullable=False),
        sa.Column("pr_number", sa.Integer, nullable=False),
        sa.Column("muted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("muted_by", sa.String(255), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("repo_id", "pr_number", name="uq_pr_mute_repo_pr"),
    )


def downgrade() -> None:
    op.drop_table("pr_mute")
