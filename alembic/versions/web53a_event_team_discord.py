"""Per-team Discord channels & roles (web53a).

- ``web_event_team_discord`` — desired-state rows for auto-created team roles
  and team channels/threads, one per (team, guild). The Web API writes desired
  state; the core bot's ``reconcile_event_team_discord`` task is the only
  place that talks to Discord and writes back ``role_id``/``channel_id``
  (the ``web_event_guilds`` pattern).
- ``web_events.team_discord_config`` — event-level JSON knobs (channels/roles
  toggles, forum target, retention, per-team overrides + captain-editable
  notification toggles). NULL = feature off.
- ``web_event_groups.team_discord_config`` — a participating clan's own knobs
  for its own guild (clan-vs-clan; no inheritance from the event level).

Revision ID: web53a_event_team_discord
Revises: web52a_event_buyins
"""
from alembic import op
import sqlalchemy as sa

revision = "web53a_event_team_discord"
down_revision = "web52a_event_buyins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_team_discord",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=False),
        sa.Column("guild_id", sa.String(32), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True),
        sa.Column("role_id", sa.String(32), nullable=True),
        sa.Column("channel_id", sa.String(32), nullable=True),
        sa.Column("channel_kind", sa.String(16), nullable=True),
        sa.Column("sync_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("member_state", sa.Text(), nullable=True),
        sa.Column("members_dirty", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("delete_after", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_web_evt_team_discord", "web_event_team_discord",
        ["team_id", "guild_id"], unique=True,
    )
    op.create_index(
        "idx_web_evt_team_discord_event", "web_event_team_discord",
        ["event_id", "sync_status"],
    )
    op.add_column("web_events", sa.Column("team_discord_config", sa.Text(), nullable=True))
    op.add_column("web_event_groups", sa.Column("team_discord_config", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("web_event_groups", "team_discord_config")
    op.drop_column("web_events", "team_discord_config")
    op.drop_index("idx_web_evt_team_discord_event", table_name="web_event_team_discord")
    op.drop_index("uq_web_evt_team_discord", table_name="web_event_team_discord")
    op.drop_table("web_event_team_discord")
