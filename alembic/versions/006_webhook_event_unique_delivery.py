"""make webhook_event.delivery_id unique (redelivery idempotency)

GitHub redelivers webhooks at-least-once. A unique delivery_id lets a repeat
insert fail fast so the handler can drop the redelivery instead of reprocessing
(and re-spending on the agent).

Revision ID: 006
Revises: 005
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_webhook_event_delivery_id", table_name="webhook_event")
    op.create_index(
        "uq_webhook_event_delivery_id", "webhook_event", ["delivery_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_webhook_event_delivery_id", table_name="webhook_event")
    op.create_index("ix_webhook_event_delivery_id", "webhook_event", ["delivery_id"])
