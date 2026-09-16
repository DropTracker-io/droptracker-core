"""Per-tier event frequency caps (web65a).

web_event_rate_limits: one row per (subscription tier, event kind) — or the
"*" all-kinds sentinel — capping how many events a group on that tier may
ACTIVATE per rolling window_days. No rows = today's behaviour (events stay
gated by the 'events' entitlement alone). Configured on /admin/event-limits;
enforced by db/event_rate_limits.py at activation.

Revision ID: web65a_event_rate_limits
Revises: web64a_group_event_managers
"""
from alembic import op
import sqlalchemy as sa

revision = "web65a_event_rate_limits"
down_revision = "web64a_group_event_managers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_rate_limits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tier_key", sa.String(length=40), nullable=False),
        # EVENT_KINDS value or "*" (all kinds combined) — the sentinel is why
        # this column is NOT NULL with no FK to web_event_types.
        sa.Column("type_key", sa.String(length=24), nullable=False,
                  server_default="*"),
        sa.Column("max_events", sa.Integer(), nullable=False),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["tier_key"], ["subscription_tiers.key"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_web_event_rate_limit", "web_event_rate_limits",
                    ["tier_key", "type_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_web_event_rate_limit", table_name="web_event_rate_limits")
    op.drop_table("web_event_rate_limits")
