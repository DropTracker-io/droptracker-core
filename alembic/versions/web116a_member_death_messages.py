"""Members' own death messages + a group's per-member block (web116a).

A member writes the line their clan sees when they die, on the website, in
Discord or in the plugin. ``player_custom_messages`` holds it per account and
per kind (only 'death' so far), as a JSON array of templates. A group opts in
with the ``allow_member_death_messages`` config key, which needs no schema,
and blocks a single member with a ``group_member_message_blocks`` row.

Purely additive: two new tables, nothing existing is read differently until a
group turns the setting on.

Revision ID: web116a_member_death_messages
Revises: web115a_board_required_tiles
"""
from alembic import op
import sqlalchemy as sa

revision = "web116a_member_death_messages"
down_revision = "web115a_board_required_tiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_custom_messages",
        # Surrogate key: the superadmin data browser addresses rows by one column.
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("message_type", sa.String(length=16), nullable=False),
        sa.Column("messages", sa.Text(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_via", sa.String(length=8), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("player_id", "message_type", name="uix_player_custom_message"),
    )
    op.create_table(
        "group_member_message_blocks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "player_id", name="uix_group_member_message_block"),
    )
    op.create_index(
        "idx_member_message_block_player", "group_member_message_blocks", ["player_id"]
    )


def downgrade() -> None:
    # Dropping the tables drops their indexes too. Dropping the player index
    # first would fail: MariaDB will not drop an index a foreign key relies on.
    op.drop_table("group_member_message_blocks")
    op.drop_table("player_custom_messages")
