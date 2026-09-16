"""Board-game core (P1): tiles, board config, turn pointers, coin wallet.

The dice-board event kind (web_events.kind = 'board_game'):

- ``web_event_tasks.difficulty``      — air|water|earth|fire rides over from
                                        the library so difficulty-tiles have a
                                        filterable roll pool.
- ``web_event_teams.coins``           — running coin balance (score pattern),
  plus ``piece_item_id``/``piece_icon_url`` — the team's game piece.
- ``web_event_board_tiles``           — the designer's output: idx along the
                                        track + fractional x/y on the image +
                                        difficulty (roll mode) or task_id (pin).
- ``web_event_board_config``          — background + §2.5 settings JSON.
- ``web_event_board_positions``       — per-team turn pointer: tile, current
                                        task, turn counter, status, mercy.
- ``web_event_coin_ledger``           — append-only audit behind teams.coins.

Revision ID: web44a_board_game_core
Revises: web43a_event_kinds
"""
from alembic import op
import sqlalchemy as sa

revision = "web44a_board_game_core"
down_revision = "web43a_event_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_event_tasks", sa.Column("difficulty", sa.String(24), nullable=True))

    op.add_column(
        "web_event_teams",
        sa.Column("coins", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("web_event_teams", sa.Column("piece_item_id", sa.Integer(), nullable=True))
    op.add_column("web_event_teams", sa.Column("piece_icon_url", sa.String(255), nullable=True))

    op.create_table(
        "web_event_board_tiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("difficulty", sa.String(24), nullable=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("web_event_tasks.id"), nullable=True),
        sa.Column("tile_kind", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("config", sa.Text(), nullable=True),
    )
    op.create_index(
        "uq_web_board_tile_event_idx",
        "web_event_board_tiles",
        ["event_id", "idx"],
        unique=True,
    )

    op.create_table(
        "web_event_board_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("background_url", sa.String(255), nullable=True),
        sa.Column("bg_width", sa.Integer(), nullable=True),
        sa.Column("bg_height", sa.Integer(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "uq_web_board_config_event", "web_event_board_config", ["event_id"], unique=True
    )

    op.create_table(
        "web_event_board_positions",
        sa.Column(
            "team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), primary_key=True
        ),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("tile_idx", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "current_task_id", sa.Integer(), sa.ForeignKey("web_event_tasks.id"), nullable=True
        ),
        sa.Column("turns_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("last_roll", sa.Text(), nullable=True),
        sa.Column("task_assigned_at", sa.DateTime(), nullable=True),
        sa.Column("mercy_deadline", sa.DateTime(), nullable=True),
        sa.Column("mercy_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("idx_web_board_pos_event", "web_event_board_positions", ["event_id"])

    op.create_table(
        "web_event_coin_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("ref_type", sa.String(16), nullable=True),
        sa.Column("ref_id", sa.BigInteger(), nullable=True),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column(
            "acted_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True
        ),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_web_coin_ledger_team", "web_event_coin_ledger", ["event_id", "team_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_web_coin_ledger_team", table_name="web_event_coin_ledger")
    op.drop_table("web_event_coin_ledger")
    op.drop_index("idx_web_board_pos_event", table_name="web_event_board_positions")
    op.drop_table("web_event_board_positions")
    op.drop_index("uq_web_board_config_event", table_name="web_event_board_config")
    op.drop_table("web_event_board_config")
    op.drop_index("uq_web_board_tile_event_idx", table_name="web_event_board_tiles")
    op.drop_table("web_event_board_tiles")
    op.drop_column("web_event_teams", "piece_icon_url")
    op.drop_column("web_event_teams", "piece_item_id")
    op.drop_column("web_event_teams", "coins")
    op.drop_column("web_event_tasks", "difficulty")
