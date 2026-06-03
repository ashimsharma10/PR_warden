"""add webhook_event durable inbox

Persists incoming actionable webhooks before the handler returns 200, so work
survives a crash/redeploy (pending rows are re-dispatched on startup).

Revision ID: 005
Revises: 003
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "webhook_event",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("action", sa.String(50), nullable=True),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_webhook_event_delivery_id", "webhook_event", ["delivery_id"])
    op.create_index("ix_webhook_event_status", "webhook_event", ["status"])


def downgrade() -> None:
    op.drop_index("ix_webhook_event_status", table_name="webhook_event")
    op.drop_index("ix_webhook_event_delivery_id", table_name="webhook_event")
    op.drop_table("webhook_event")
