"""hardcore_players / hardcore_submissions / hardcore_drops (web150a).

Hardcore Worlds (launched 2026-11-04) are fresh one-life characters with their
own economy. Plugin 6.1.19+ tags their submissions ``world_type=HARDCORE_MAIN``;
until now intake discarded every non-main, non-seasonal world type. These
tables store them apart from the main game (``db/models/hardcore.py``):

- ``hardcore_players``: the character, by account hash. Never a ``players`` row.
- ``hardcore_submissions``: every submission as it arrived (auth key stripped),
  unique per plugin GUID.
- ``hardcore_drops``: typed copy of drops, valued at the client's price.

New, empty tables; nothing else is touched.

Revision ID: web150a_hardcore_worlds
Revises: web149a_ticket_pings_muted
"""
from alembic import op
import sqlalchemy as sa

revision = "web150a_hardcore_worlds"
down_revision = "web149a_ticket_pings_muted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hardcore_players",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_hash", sa.String(100), nullable=False),
        sa.Column("player_name", sa.String(64), nullable=False),
        sa.Column("account_type", sa.String(32), nullable=True),
        sa.Column("plugin_version", sa.String(20), nullable=True),
        sa.Column("first_seen", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_hardcore_players_account_hash", "hardcore_players",
                    ["account_hash"], unique=True)
    op.create_index("ix_hardcore_players_player_name", "hardcore_players", ["player_name"])

    op.create_table(
        "hardcore_submissions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("hardcore_players.id"), nullable=False),
        sa.Column("submission_type", sa.String(32), nullable=False),
        sa.Column("unique_id", sa.String(255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("plugin_version", sa.String(20), nullable=True),
        sa.Column("used_api", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("date_added", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("partition", sa.Integer(), nullable=False),
    )
    op.create_index("uq_hardcore_submissions_unique_id", "hardcore_submissions",
                    ["unique_id"], unique=True)
    op.create_index("ix_hardcore_submissions_player_type_date", "hardcore_submissions",
                    ["player_id", "submission_type", "date_added"])
    op.create_index("ix_hardcore_submissions_type_date", "hardcore_submissions",
                    ["submission_type", "date_added"])
    op.create_index("ix_hardcore_submissions_partition", "hardcore_submissions", ["partition"])

    op.create_table(
        "hardcore_drops",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("submission_id", sa.BigInteger(),
                  sa.ForeignKey("hardcore_submissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("hardcore_players.id"), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("item_name", sa.String(255), nullable=False),
        sa.Column("npc_name", sa.String(255), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_value", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("kill_count", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("partition", sa.Integer(), nullable=False),
    )
    op.create_index("uq_hardcore_drops_submission_id", "hardcore_drops",
                    ["submission_id"], unique=True)
    op.create_index("ix_hardcore_drops_player_date", "hardcore_drops", ["player_id", "date_added"])
    op.create_index("ix_hardcore_drops_partition", "hardcore_drops", ["partition"])
    op.create_index("ix_hardcore_drops_item", "hardcore_drops", ["item_id"])


def downgrade() -> None:
    op.drop_table("hardcore_drops")
    op.drop_table("hardcore_submissions")
    op.drop_table("hardcore_players")
