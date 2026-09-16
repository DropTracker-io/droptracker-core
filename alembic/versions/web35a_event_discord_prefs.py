"""Event Discord preferences: draft gating, ping roles, CvC mirror opt-in.

- web_events.discord_event_policy — when the mirrored Discord scheduled event
  goes live ('on_activate' default: drafts create nothing on Discord;
  'immediate': at creation). Fixes drafts surfacing on Discord instantly and
  abandoned drafts resurrecting later (the 2026-07-11 "duplicate event" —
  a failed draft row retried by an invite-accept sync).
- web_events.ping_config — JSON {ping_key: [role ids]} (EVENT_PING_KEYS):
  roles the bot mentions on the scheduled-event companion message /
  lifecycle announcements.
- web_event_groups.mirror_discord_event — accept-time opt-in for a
  clan-vs-clan participant to mirror the scheduled event into their own
  linked guild (never on by default).

See services/event_scheduled_events.py + web_api/routes/event_discord.py.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "web35a_event_discord_prefs"
down_revision = "web34a_site_redirects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column(
            "discord_event_policy",
            sa.String(length=16),
            nullable=False,
            server_default="on_activate",
        ),
    )
    op.add_column("web_events", sa.Column("ping_config", sa.Text(), nullable=True))
    op.add_column(
        "web_event_groups",
        sa.Column(
            "mirror_discord_event",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("web_event_groups", "mirror_discord_event")
    op.drop_column("web_events", "ping_config")
    op.drop_column("web_events", "discord_event_policy")
