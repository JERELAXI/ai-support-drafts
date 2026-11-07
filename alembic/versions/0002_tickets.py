"""tickets

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = (
    "received",
    "generating",
    "drafted",
    "approved",
    "sending",
    "sent",
    "generation_failed",
    "rejected",
    "send_failed",
)


def upgrade() -> None:
    ticket_status = postgresql.ENUM(*STATUSES, name="ticket_status", create_type=False)
    ticket_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tickets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("customer_email", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply_url", sa.Text(), nullable=False),
        sa.Column("status", ticket_status, server_default="received", nullable=False),
        sa.Column("draft", sa.Text()),
        sa.Column("draft_sources", postgresql.JSONB()),
        sa.Column("final_message", sa.Text()),
        sa.Column("attempts_generate", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempts_send", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("event_id", name="uq_tickets_event_id"),
    )
    op.create_index("ix_tickets_status_updated_at", "tickets", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_tickets_status_updated_at", table_name="tickets")
    op.drop_table("tickets")
    postgresql.ENUM(name="ticket_status").drop(op.get_bind(), checkfirst=True)
