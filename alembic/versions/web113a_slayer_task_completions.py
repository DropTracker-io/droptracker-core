"""Slayer task completions (web113a).

One row per slayer task a player completes, reported by the plugin — the source
for ``slayer_target`` event goals ("25 tasks", "10 from Duradel"). Nothing else
in the schema records it: hiscores and Wise Old Man expose no task count, so
this table is the only place the number can come from.

``master_id`` is the RAW SLAYER_MASTER varbit value and is kept even though a
name column sits beside it: only two of the ten ids are confirmed at the time
of writing (utils/slayer_masters.py), and the raw id is what lets a mapping
correction fix history instead of only the future.

Single table across world types (``world_type`` column), like deaths and
diaries. ``unique_id`` is UNIQUE — the plugin's guid is the dedupe key on
replay (data/submissions/common.py ensure_can_create), the web84a lesson.

Revision ID: web113a_slayer_task_completions
Revises: web112a_event_tasks_visibility
"""
from alembic import op
import sqlalchemy as sa

revision = "web113a_slayer_task_completions"
down_revision = "web112a_event_tasks_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slayer_task_completions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("task_name", sa.String(length=120), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("boss_id", sa.Integer(), nullable=True),
        sa.Column("master_id", sa.SmallInteger(), nullable=True),
        sa.Column("master_name", sa.String(length=40), nullable=True),
        sa.Column("area_id", sa.Integer(), nullable=True),
        sa.Column("amount_initial", sa.Integer(), nullable=True),
        sa.Column("amount_killed", sa.Integer(), nullable=True),
        sa.Column("streak", sa.Integer(), nullable=True),
        sa.Column("points_awarded", sa.Integer(), nullable=True),
        sa.Column("points_total", sa.Integer(), nullable=True),
        sa.Column("xp_gained", sa.Integer(), nullable=True),
        sa.Column("completion_message", sa.String(length=255), nullable=True),
        sa.Column("world_type", sa.String(length=20), nullable=False,
                  server_default="main"),
        sa.Column("timestamp", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(length=300), nullable=True),
        sa.Column("video_url", sa.String(length=500), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=True,
                  server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), nullable=True,
                  server_default=sa.text("0")),
        sa.Column("unique_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_slayer_task_completions_player_date",
                    "slayer_task_completions", ["player_id", "date_added"])
    op.create_index("ix_slayer_task_completions_date_added",
                    "slayer_task_completions", ["date_added"])
    op.create_index("ix_slayer_task_completions_master_id",
                    "slayer_task_completions", ["master_id"])
    op.create_index("ix_slayer_task_completions_unique_id",
                    "slayer_task_completions", ["unique_id"], unique=True)


def downgrade() -> None:
    op.drop_table("slayer_task_completions")
