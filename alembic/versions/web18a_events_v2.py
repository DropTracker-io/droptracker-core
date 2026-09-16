"""Events schema v2 (backend Task 15, events-prd.md).

Engine foundation for the events platform: formation modes, per-task/per-event
manual-confirmation flags, explicit lifecycle timestamps, per-event Discord
guild/channel config, the completion ledger + progress rollup written by the
event consumer worker, and the curated task library seeded from the legacy
BoardGame task store. Additive only — Task-14 tables/columns are unchanged.
"""

from alembic import op
import sqlalchemy as sa


revision = "web18a_events_v2"
down_revision = "web17a_badges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- web_events: formation / verification / lifecycle / discord / bingo config
    op.add_column("web_events", sa.Column("formation_mode", sa.String(length=16), nullable=False, server_default="admin_assign"))
    op.add_column("web_events", sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("web_events", sa.Column("join_code", sa.String(length=32), nullable=True))
    op.add_column("web_events", sa.Column("discord_guild_id", sa.String(length=32), nullable=True))
    op.add_column("web_events", sa.Column("board_size", sa.Integer(), nullable=False, server_default="5"))
    op.add_column("web_events", sa.Column("bonus_line_points", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("web_events", sa.Column("bonus_blackout_points", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("web_events", sa.Column("activated_at", sa.DateTime(), nullable=True))
    op.add_column("web_events", sa.Column("ended_at", sa.DateTime(), nullable=True))

    # --- web_event_tasks: per-task confirmation + typed JSON config (any_of/assembly/…)
    op.add_column("web_event_tasks", sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("web_event_tasks", sa.Column("config", sa.Text(), nullable=True))

    # --- web_event_team_members: joined_at is the no-retroactive-credit cutoff (PRD D10)
    op.add_column("web_event_team_members", sa.Column("joined_at", sa.DateTime(), nullable=False, server_default=sa.func.now()))

    # --- completion ledger: one row per qualifying submission or manual admin action
    op.create_table(
        "web_event_completions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("player_id", sa.Integer(), nullable=True),
        # auto|pending|confirmed|rejected|manual|revoked
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        # drop|pb|clog|ca|experience|manual|bonus
        sa.Column("source_type", sa.String(length=16), nullable=True),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("submission_guid", sa.String(length=64), nullable=True),
        sa.Column("proof_url", sa.String(length=255), nullable=True),
        sa.Column("acted_by_user_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["event_id"], ["web_events.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["web_event_tasks.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["web_event_teams.id"]),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.ForeignKeyConstraint(["acted_by_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_web_evt_completion_event", "web_event_completions", ["event_id", "status"], unique=False)
    op.create_index("idx_web_evt_completion_task_team", "web_event_completions", ["task_id", "team_id"], unique=False)
    # Idempotency under queue replay; MySQL permits multiple NULL guids (manual rows exempt).
    op.create_index("uq_web_evt_completion_src", "web_event_completions", ["task_id", "team_id", "submission_guid"], unique=True)

    # --- progress rollup: one row per (task, team), folded from non-pending ledger rows
    op.create_table(
        "web_event_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["web_events.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["web_event_tasks.id"]),
        sa.ForeignKeyConstraint(["team_id"], ["web_event_teams.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_web_evt_progress", "web_event_progress", ["task_id", "team_id"], unique=True)
    op.create_index("idx_web_evt_progress_event", "web_event_progress", ["event_id"], unique=False)

    # --- per-event Discord destinations (announcements|completions|leaderboard|admin)
    op.create_table(
        "web_event_channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["web_events.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_web_event_channel", "web_event_channels", ["event_id", "kind"], unique=True)

    # --- curated task presets (seeded from legacy games/events/task_store/default.json)
    op.create_table(
        "web_event_task_library",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column("target", sa.String(length=120), nullable=True),
        sa.Column("target_value", sa.Integer(), nullable=True),
        sa.Column("default_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("difficulty", sa.String(length=24), nullable=True),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False, server_default="legacy_v1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_web_evt_library_name_source", "web_event_task_library", ["name", "source"], unique=True)


def downgrade() -> None:
    op.drop_table("web_event_task_library")
    op.drop_table("web_event_channels")
    op.drop_table("web_event_progress")
    op.drop_table("web_event_completions")
    op.drop_column("web_event_team_members", "joined_at")
    op.drop_column("web_event_tasks", "config")
    op.drop_column("web_event_tasks", "requires_confirmation")
    for col in (
        "ended_at",
        "activated_at",
        "bonus_blackout_points",
        "bonus_line_points",
        "board_size",
        "discord_guild_id",
        "join_code",
        "requires_confirmation",
        "formation_mode",
    ):
        op.drop_column("web_events", col)
