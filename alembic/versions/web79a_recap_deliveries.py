"""Recap delivery ledger (web79a).

``recap_deliveries`` records that a recap card was *sent*, where
``recap_snapshots`` records only that one was *built*. It answers two questions
the snapshot table cannot:

* idempotency — a restart mid-run or a catch-up tick must not post a clan's card
  twice or DM the same person twice for one period;
* entitlement — every user gets one unsolicited recap (their first) and must opt
  in for the rest, which is "has this user_id ever been sent one" rather than a
  separate boolean that could drift.

``is_test`` is part of the unique key so a redirected rollout send (everything
pointed at one test recipient) cannot consume the real recipient's slot.

Revision ID: web79a_recap_deliveries
Revises: web78a_team_voice_channel
"""
from alembic import op
import sqlalchemy as sa

revision = "web79a_recap_deliveries"
down_revision = "web78a_team_voice_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recap_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("target_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="sent"),
        sa.Column("message_id", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("is_test", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "sent_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope", "subject_id", "period", "kind", "is_test",
            name="uq_recap_delivery_subject_period_kind",
        ),
    )
    op.create_index(
        "idx_recap_delivery_user", "recap_deliveries", ["user_id", "is_test"]
    )
    op.create_index(
        "idx_recap_delivery_period", "recap_deliveries", ["period", "kind"]
    )


def downgrade() -> None:
    op.drop_index("idx_recap_delivery_period", table_name="recap_deliveries")
    op.drop_index("idx_recap_delivery_user", table_name="recap_deliveries")
    op.drop_table("recap_deliveries")
