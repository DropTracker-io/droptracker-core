"""Per-group always-announce list: items/NPCs a clan always wants posted (web109a).

The inverse of web101a's notification blacklist. A leader lists an item or an
NPC and drops of that item — or from that NPC — are announced in the group's
Discord even below the group's minimum_value_to_notify. Built for the "notable"
zero-value items the plugin already force-screenshots (kits, dyes, untradeable
pieces) that would otherwise never clear the value threshold.

Same shape as the blacklist table on purpose: ``match_key`` stores the
normalized comparison form (``db.notification_always_list``, which imports the
blacklist's normalizers) so the uniqueness rule and the pipeline lookup agree
by construction, and ``game_id`` is not a foreign key because a hand-typed name
the catalog has not seen yet is still a valid entry.

Revision ID: web109a_notification_always_list
Revises: web108a_player_npc_kc
"""
from alembic import op
import sqlalchemy as sa

revision = "web109a_notification_always_list"
down_revision = "web108a_player_npc_kc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_notification_always_list",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(length=8), nullable=False),
        sa.Column("entry_name", sa.String(length=125), nullable=False),
        sa.Column("match_key", sa.String(length=125), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=True),
        sa.Column("added_by_user_id", sa.Integer(), nullable=True),
        sa.Column("date_added", sa.DateTime(), server_default=sa.func.now(), nullable=True),
        sa.Column("date_updated", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "entry_type", "match_key", name="uix_group_always_list_entry"
        ),
    )
    op.create_index(
        "idx_group_always_list_group", "group_notification_always_list", ["group_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "idx_group_always_list_group", table_name="group_notification_always_list"
    )
    op.drop_table("group_notification_always_list")
