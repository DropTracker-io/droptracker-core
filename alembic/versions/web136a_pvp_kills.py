"""PvP kill tracking: pvp_kills, pvp_kill_items, pvp_kill_groups (web136a).

Player-kill loot used to run through the NPC drop pipeline, which rejected it
because no NPC carries the victim's name (and booked it as an NPC drop when
one did). These tables hold it on its own: one row per kill, its items, and
whether it counts for each of the killer's groups. See db/models/pvp.py.

The group settings that go with it (channel_id_to_post_pvp and the pvp_*
keys) are group_configurations rows validated by the registry, so they need
no schema change.

Revision ID: web136a_pvp_kills
Revises: web135a_submission_api_keys
"""
import sqlalchemy as sa
from alembic import op

revision = "web136a_pvp_kills"
down_revision = "web135a_submission_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pvp_kills",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("victim_name", sa.String(64), nullable=False),
        sa.Column("victim_player_id", sa.Integer(), nullable=True),
        sa.Column("victim_combat_level", sa.SmallInteger(), nullable=True),
        sa.Column("world", sa.SmallInteger(), nullable=True),
        sa.Column("wilderness_level", sa.SmallInteger(), nullable=True),
        sa.Column("region_id", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(125), nullable=True),
        sa.Column("total_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("world_type", sa.String(20), nullable=False, server_default="main"),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("unique_id", sa.String(255), nullable=True),
        sa.Column("used_api", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("date_added", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("partition", sa.Integer(), nullable=False),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("excluded_reason", sa.String(48), nullable=True),
        sa.Column("plugin_version", sa.String(20), nullable=True),
    )
    op.create_index("uq_pvp_kills_unique_id", "pvp_kills", ["unique_id"], unique=True)
    op.create_index("ix_pvp_kills_player_date", "pvp_kills", ["player_id", "date_added"])
    op.create_index("ix_pvp_kills_victim_date", "pvp_kills", ["victim_name", "date_added"])
    op.create_index("ix_pvp_kills_victim_player_id", "pvp_kills", ["victim_player_id"])
    op.create_index("ix_pvp_kills_partition", "pvp_kills", ["partition"])

    op.create_table(
        "pvp_kill_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kill_id", sa.BigInteger(),
                  sa.ForeignKey("pvp_kills.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("item_name", sa.String(125), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("value", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_pvp_kill_items_kill_id", "pvp_kill_items", ["kill_id"])
    op.create_index("ix_pvp_kill_items_item_id", "pvp_kill_items", ["item_id"])

    op.create_table(
        "pvp_kill_groups",
        sa.Column("kill_id", sa.BigInteger(),
                  sa.ForeignKey("pvp_kills.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("group_id", sa.Integer(), primary_key=True),
        sa.Column("counted", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("reason", sa.String(32), nullable=True),
    )
    op.create_index("ix_pvp_kill_groups_group_counted", "pvp_kill_groups",
                    ["group_id", "counted", "kill_id"])


def downgrade() -> None:
    op.drop_table("pvp_kill_groups")
    op.drop_table("pvp_kill_items")
    op.drop_table("pvp_kills")
