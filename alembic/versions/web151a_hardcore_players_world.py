"""Hardcore characters become players rows; seasonal-style mirror tables (web151a).

web150a stored Hardcore Worlds submissions on their own, against a separate
``hardcore_players`` table. Groups now announce, board, score and run events on
them (owner decision 2026-10-27), and every one of those features is keyed on
``players.player_id``, so a hardcore character becomes a ``players`` row with
``world = 'hardcore'`` and its submissions run through the normal processors
like seasonal ones (db/models/hardcore.py).

- ``players.world``: 'main' for every existing row (instant ADD COLUMN), indexed.
- web150a's ``hardcore_players`` / ``hardcore_submissions`` / ``hardcore_drops``
  are dropped. They were never written in production (plugin 6.1.19 was not
  yet released), and this refuses to drop them if they hold any rows.
- New: ``hardcore_submissions`` (raw archive, now keyed to players) and mirrors
  of the seasonal tables: ``hardcore_drops``, ``hardcore_personal_best``,
  ``hardcore_collection``, ``hardcore_combat_achievement``,
  ``hardcore_player_pets``, ``hardcore_quest_completions``.

Revision ID: web151a_hardcore_players_world
Revises: web150a_hardcore_worlds
"""
from alembic import op
import sqlalchemy as sa

revision = "web151a_hardcore_players_world"
down_revision = "web150a_hardcore_worlds"
branch_labels = None
depends_on = None

_OLD = ("hardcore_drops", "hardcore_submissions", "hardcore_players")


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    have = _tables()
    for table in _OLD:
        if table in have:
            rows = bind.execute(sa.text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
            if rows:
                raise RuntimeError(f"{table} holds {rows} rows; refusing to drop it")
    for table in _OLD:
        if table in have:
            op.drop_table(table)

    columns = {c["name"] for c in sa.inspect(bind).get_columns("players")}
    if "world" not in columns:
        op.add_column("players", sa.Column("world", sa.String(16), nullable=False, server_default="main"))
        op.create_index("ix_players_world", "players", ["world"])

    op.create_table(
        "hardcore_submissions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("submission_type", sa.String(32), nullable=False),
        sa.Column("unique_id", sa.String(255), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("plugin_version", sa.String(20), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("partition", sa.Integer(), nullable=False),
    )
    op.create_index("uq_hardcore_submissions_unique_id", "hardcore_submissions", ["unique_id"], unique=True)
    op.create_index("ix_hardcore_submissions_player_type_date", "hardcore_submissions",
                    ["player_id", "submission_type", "date_added"])
    op.create_index("ix_hardcore_submissions_type_date", "hardcore_submissions",
                    ["submission_type", "date_added"])
    op.create_index("ix_hardcore_submissions_partition", "hardcore_submissions", ["partition"])

    op.create_table(
        "hardcore_drops",
        sa.Column("drop_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id")),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("date_added", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("npc_id", sa.Integer(), sa.ForeignKey("npc_list.npc_id")),
        sa.Column("date_updated", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("value", sa.BigInteger()),
        sa.Column("quantity", sa.Integer()),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("video_url", sa.String(500), nullable=True),
        sa.Column("authed", sa.Boolean(), server_default="0"),
        sa.Column("used_api", sa.Boolean(), server_default="0"),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("partition", sa.Integer()),
        sa.Column("unique_id", sa.String(255), nullable=True),
        sa.Column("kill_count", sa.Integer(), nullable=True),
    )
    for col in ("item_id", "player_id", "date_added", "npc_id", "partition"):
        op.create_index(f"ix_hardcore_drops_{col}", "hardcore_drops", [col])
    op.create_index("ix_hardcore_drops_unique_id", "hardcore_drops", ["unique_id"])

    op.create_table(
        "hardcore_personal_best",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id")),
        sa.Column("npc_id", sa.Integer(), sa.ForeignKey("npc_list.npc_id")),
        sa.Column("kill_time", sa.Integer(), nullable=False),
        sa.Column("personal_best", sa.Integer(), nullable=False),
        sa.Column("team_size", sa.String(15), nullable=False, server_default="Solo"),
        sa.Column("new_pb", sa.Boolean(), server_default="0"),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("video_url", sa.String(500), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), server_default="0"),
        sa.Column("unique_id", sa.String(255), nullable=True),
    )
    op.create_index("ix_hardcore_personal_best_player_id", "hardcore_personal_best", ["player_id"])
    op.create_index("ix_hardcore_personal_best_unique_id", "hardcore_personal_best", ["unique_id"])

    op.create_table(
        "hardcore_collection",
        sa.Column("log_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("npc_id", sa.Integer(), sa.ForeignKey("npc_list.npc_id"), nullable=False),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("reported_slots", sa.Integer()),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("video_url", sa.String(500), nullable=True),
        sa.Column("date_added", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("date_updated", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), server_default="0"),
        sa.Column("unique_id", sa.String(255), nullable=True),
    )
    for col in ("item_id", "player_id", "date_added", "unique_id"):
        op.create_index(f"ix_hardcore_collection_{col}", "hardcore_collection", [col])

    op.create_table(
        "hardcore_combat_achievement",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id")),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("video_url", sa.String(500), nullable=True),
        sa.Column("date_added", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), server_default="0"),
        sa.Column("unique_id", sa.String(255), nullable=True),
    )
    for col in ("player_id", "date_added", "unique_id"):
        op.create_index(f"ix_hardcore_combat_achievement_{col}", "hardcore_combat_achievement", [col])

    op.create_table(
        "hardcore_player_pets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id")),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id")),
        sa.Column("date_added", sa.DateTime(), nullable=True),
        sa.Column("unique_id", sa.String(255), nullable=True),
        sa.Column("pet_name", sa.String(255), nullable=False),
    )
    for col in ("player_id", "unique_id"):
        op.create_index(f"ix_hardcore_player_pets_{col}", "hardcore_player_pets", [col])

    op.create_table(
        "hardcore_quest_completions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("quest_name", sa.String(255), nullable=False),
        sa.Column("quests_completed", sa.Integer(), nullable=True),
        sa.Column("total_quests", sa.Integer(), nullable=True),
        sa.Column("completion_percentage", sa.String(20), nullable=True),
        sa.Column("quest_points", sa.Integer(), nullable=True),
        sa.Column("total_quest_points", sa.Integer(), nullable=True),
        sa.Column("qp_percentage", sa.String(20), nullable=True),
        sa.Column("timestamp", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(300), nullable=True),
        sa.Column("video_url", sa.String(500), nullable=True),
        sa.Column("date_added", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), server_default="0"),
        sa.Column("unique_id", sa.String(255), nullable=True),
    )
    op.create_index("ix_hardcore_quest_completions_player_id", "hardcore_quest_completions", ["player_id"])
    op.create_index("ix_hardcore_quest_completions_date_added", "hardcore_quest_completions", ["date_added"])
    op.create_index("uq_hardcore_quest_completions_unique_id", "hardcore_quest_completions",
                    ["unique_id"], unique=True)


def downgrade() -> None:
    for table in ("hardcore_quest_completions", "hardcore_player_pets", "hardcore_combat_achievement",
                  "hardcore_collection", "hardcore_personal_best", "hardcore_drops", "hardcore_submissions"):
        op.drop_table(table)
    op.drop_index("ix_players_world", table_name="players")
    op.drop_column("players", "world")
