"""Badge system: badges catalog + player_badges awards, with v1 seed rows.

Also adds an index on personal_best (npc_id, team_size, personal_best) so the
boss-record evaluator's per-slot "fastest time" lookups are indexed.

Dedupe design notes live in db/models/badge.py.
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "web17a_badges"
down_revision = "web16a_entitlements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "badges",
        sa.Column("badge_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("icon_url", sa.String(length=512), nullable=True),
        sa.Column("icon_emoji", sa.String(length=16), nullable=True),
        sa.Column("tone", sa.String(length=16), nullable=False, server_default="gold"),
        sa.Column("semantic", sa.String(length=16), nullable=False, server_default="permanent"),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("criteria", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("badge_id"),
        sa.UniqueConstraint("key", name="uix_badge_key"),
    )

    op.create_table(
        "player_badges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("badge_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("group_key", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("slot_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("active_key", sa.String(length=64), nullable=True),
        sa.Column("awarded_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("lost_at", sa.DateTime(), nullable=True),
        sa.Column("awarded_by", sa.Integer(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["badge_id"], ["badges.badge_id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["awarded_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_player_badges_active",
        "player_badges",
        ["badge_id", "group_key", "active_key"],
        unique=True,
    )
    op.create_index(
        "idx_player_badges_player_status", "player_badges", ["player_id", "status"]
    )
    op.create_index(
        "idx_player_badges_badge_status", "player_badges", ["badge_id", "status"]
    )

    op.create_index(
        "ix_pb_npc_team_time",
        "personal_best",
        ["npc_id", "team_size", "personal_best"],
    )

    badges = sa.table(
        "badges",
        sa.column("key", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("icon_emoji", sa.String),
        sa.column("tone", sa.String),
        sa.column("semantic", sa.String),
        sa.column("criteria", sa.Text),
    )
    op.bulk_insert(
        badges,
        [
            {
                "key": "daily_loot_champion",
                "name": "Daily Loot Champion",
                "description": "Received the most loot of any tracked player on a calendar day.",
                "icon_emoji": "\U0001F451",  # crown
                "tone": "gold",
                "semantic": "permanent",
                "criteria": json.dumps({"type": "daily_champion"}),
            },
            {
                "key": "loot_streak_7",
                "name": "Week-Long Grinder",
                "description": "Logged loot every day for 7 days in a row.",
                "icon_emoji": "\U0001F525",  # fire
                "tone": "green",
                "semantic": "permanent",
                "criteria": json.dumps({"type": "loot_streak", "days": 7}),
            },
            {
                "key": "loot_streak_30",
                "name": "Iron Discipline",
                "description": "Logged loot every day for 30 days in a row.",
                "icon_emoji": "\U0001F4C5",  # calendar
                "tone": "purple",
                "semantic": "permanent",
                "criteria": json.dumps({"type": "loot_streak", "days": 30}),
            },
            {
                "key": "boss_record",
                "name": "Boss Record Holder",
                "description": "Holds the fastest tracked kill time at a boss and team size.",
                "icon_emoji": "\U0001F3C6",  # trophy
                "tone": "ember",
                "semantic": "held",
                "criteria": json.dumps({"type": "boss_record"}),
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_pb_npc_team_time", table_name="personal_best")
    op.drop_index("idx_player_badges_badge_status", table_name="player_badges")
    op.drop_index("idx_player_badges_player_status", table_name="player_badges")
    op.drop_index("ux_player_badges_active", table_name="player_badges")
    op.drop_table("player_badges")
    op.drop_table("badges")
