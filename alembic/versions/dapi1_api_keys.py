"""Data API (v2): api_key_tiers + api_keys (dev-tracker #15).

Seeds the three launch tiers. Every key starts on 'standard' (the floor) by
owner decision 2026-08-27 — premium groups included; admins promote keys once
usage is verified, and per-key nullable overrides handle one-off custom
limits.

Revision ID: dapi1_api_keys
Revises: web103a_event_team_short_tag
"""
import sqlalchemy as sa
from alembic import op

revision = "dapi1_api_keys"
down_revision = "web103a_event_team_short_tag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_key_tiers",
        sa.Column("tier_key", sa.String(32), primary_key=True),
        sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("requests_per_min", sa.Integer, nullable=False),
        sa.Column("cost_units_per_min", sa.Integer, nullable=False),
        sa.Column("requests_per_day", sa.Integer, nullable=False),
        sa.Column("max_concurrency", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default=sa.text("0")),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(8), nullable=False),
        sa.Column("label", sa.String(64), nullable=False, server_default=""),
        sa.Column("owner_user_id", sa.Integer,
                  sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("group_id", sa.Integer,
                  sa.ForeignKey("groups.group_id"), nullable=True),
        sa.Column("tier_key", sa.String(32),
                  sa.ForeignKey("api_key_tiers.tier_key"),
                  nullable=False, server_default="standard"),
        sa.Column("requests_per_min", sa.Integer, nullable=True),
        sa.Column("cost_units_per_min", sa.Integer, nullable=True),
        sa.Column("requests_per_day", sa.Integer, nullable=True),
        sa.Column("max_concurrency", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("created_by_user_id", sa.Integer, nullable=True),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("expires_at", sa.DateTime, nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.CheckConstraint("(owner_user_id IS NULL) != (group_id IS NULL)",
                           name="ck_api_keys_one_owner"),
    )
    op.create_index("idx_api_keys_owner_user", "api_keys", ["owner_user_id"])
    op.create_index("idx_api_keys_group", "api_keys", ["group_id"])

    tiers = sa.table(
        "api_key_tiers",
        sa.column("tier_key", sa.String),
        sa.column("display_name", sa.String),
        sa.column("requests_per_min", sa.Integer),
        sa.column("cost_units_per_min", sa.Integer),
        sa.column("requests_per_day", sa.Integer),
        sa.column("max_concurrency", sa.Integer),
        sa.column("enabled", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(tiers, [
        {"tier_key": "standard", "display_name": "Standard",
         "requests_per_min": 60, "cost_units_per_min": 300,
         "requests_per_day": 10_000, "max_concurrency": 4,
         "enabled": True, "sort_order": 0},
        {"tier_key": "elevated", "display_name": "Elevated",
         "requests_per_min": 300, "cost_units_per_min": 2_000,
         "requests_per_day": 100_000, "max_concurrency": 8,
         "enabled": True, "sort_order": 1},
        {"tier_key": "partner", "display_name": "Partner",
         "requests_per_min": 1_200, "cost_units_per_min": 10_000,
         "requests_per_day": 1_000_000, "max_concurrency": 16,
         "enabled": True, "sort_order": 2},
    ])


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_table("api_key_tiers")
