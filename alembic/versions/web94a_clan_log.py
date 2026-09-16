"""Clan Log catalog + progress ledger (web94a).

Three tables behind the group unique-completion board:

``clan_log_sections`` / ``clan_log_items`` are the curated catalog of obtainable
slots (which uniques exist, and which boss they come from). Curated rather than
derived because the wiki drop tables in ``xenforo.dt_npc_loot`` cannot express
"unique" — raid rarities are conditional on a unique roll and new content lands
with no rows at all.

``clan_log_firsts`` is the progress ledger: one row per (group, item, month)
recording the first member to obtain that item that month. Month granularity is
what lets the month / year / all-time views fold out of a single table instead
of being backfilled per period.

Revision ID: web94a_clan_log
Revises: web93a_team_loot_posts
"""
from alembic import op
import sqlalchemy as sa

revision = "web94a_clan_log"
down_revision = "web93a_team_loot_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clan_log_sections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("label", sa.String(96), nullable=False),
        sa.Column("category", sa.String(32), nullable=False, server_default="other"),
        sa.Column("npc_keys", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_clan_log_section_slug"),
    )
    op.create_index("idx_clan_log_section_order", "clan_log_sections",
                    ["category", "sort_order"])

    op.create_table(
        "clan_log_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("section_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("item_name", sa.String(125), nullable=False),
        sa.Column("variant_item_ids", sa.Text(), nullable=True),
        sa.Column("attributable", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("source_hint", sa.String(8), nullable=False, server_default="drop"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["section_id"], ["clan_log_sections.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("section_id", "item_id", name="uq_clan_log_section_item"),
    )
    op.create_index("idx_clan_log_item_lookup", "clan_log_items", ["item_id"])

    op.create_table(
        "clan_log_firsts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("obtained_at", sa.DateTime(), nullable=False),
        sa.Column("drop_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(8), nullable=False, server_default="drop"),
        sa.Column("proof_url", sa.String(500), nullable=True),
        sa.Column("obtained_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("player_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "item_id", "month", name="uq_clan_log_first"),
    )
    op.create_index("idx_clan_log_first_group_month", "clan_log_firsts",
                    ["group_id", "month"])


def downgrade() -> None:
    op.drop_index("idx_clan_log_first_group_month", table_name="clan_log_firsts")
    op.drop_table("clan_log_firsts")
    op.drop_index("idx_clan_log_item_lookup", table_name="clan_log_items")
    op.drop_table("clan_log_items")
    op.drop_index("idx_clan_log_section_order", table_name="clan_log_sections")
    op.drop_table("clan_log_sections")
