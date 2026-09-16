"""Team leadership + per-group clan-vs-clan Discord config.

- ``web_events.leadership_config``       — JSON {"enabled", "co_leaders",
                                            "selection": "admin"|"election"}.
- ``web_events.per_group_discord``       — CvC: each accepted clan configures
                                            its own channels + verbosity.
- ``web_event_team_members.role``        — "leader"/"co_leader"/NULL.
- ``web_event_leader_votes``             — one live vote per (event, voter);
                                            strict plurality elects.
- ``web_event_channels.group_id``        — owning clan for per-group configs
                                            (NULL = shared/host set); unique
                                            key widens to (event, kind, group).
- ``web_event_groups.message_config``    — per-clan verbosity override.
- ``web_event_groups.discord_guild_id``  — the clan's own guild for pickers.

Revision ID: web48a_leadership_pergroup_discord
Revises: web47a_event_player_points
"""
from alembic import op
import sqlalchemy as sa

revision = "web48a_leadership_pergroup_discord"
down_revision = "web47a_event_player_points"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_events", sa.Column("leadership_config", sa.Text(), nullable=True))
    op.add_column("web_events", sa.Column(
        "per_group_discord", sa.Boolean(), nullable=False, server_default="0"))

    op.add_column("web_event_team_members", sa.Column("role", sa.String(16), nullable=True))

    op.create_table(
        "web_event_leader_votes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=False),
        sa.Column("voter_player_id", sa.Integer(),
                  sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("candidate_player_id", sa.Integer(),
                  sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("uq_web_evt_leader_vote", "web_event_leader_votes",
                    ["event_id", "voter_player_id"], unique=True)
    op.create_index("idx_web_evt_leader_vote_team", "web_event_leader_votes",
                    ["event_id", "team_id"])

    op.add_column("web_event_channels", sa.Column(
        "group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True))
    # The old (event_id, kind) unique index backs the event_id FK, so MySQL
    # refuses to drop it first (errno 1553) — create the widened replacement
    # under a NEW name, then drop the old one.
    op.create_index("uq_web_event_channel_grp", "web_event_channels",
                    ["event_id", "kind", "group_id"], unique=True)
    op.drop_index("uq_web_event_channel", table_name="web_event_channels")

    op.add_column("web_event_groups", sa.Column("message_config", sa.Text(), nullable=True))
    op.add_column("web_event_groups", sa.Column("discord_guild_id", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("web_event_groups", "discord_guild_id")
    op.drop_column("web_event_groups", "message_config")
    op.create_index("uq_web_event_channel", "web_event_channels",
                    ["event_id", "kind"], unique=True)
    op.drop_index("uq_web_event_channel_grp", table_name="web_event_channels")
    op.drop_column("web_event_channels", "group_id")
    op.drop_index("idx_web_evt_leader_vote_team", table_name="web_event_leader_votes")
    op.drop_index("uq_web_evt_leader_vote", table_name="web_event_leader_votes")
    op.drop_table("web_event_leader_votes")
    op.drop_column("web_event_team_members", "role")
    op.drop_column("web_events", "per_group_discord")
    op.drop_column("web_events", "leadership_config")
