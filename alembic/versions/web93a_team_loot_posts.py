"""Team-channel lootboard posts (web93a).

Delivery half of the per-team event lootboards (``lootboard/team_boards.py``):
each team channel gets a SECOND bot-owned message, directly beneath the web54a
primary board post, holding the team's generated lootboard PNG.
``loot_message_id`` is that message, ``loot_state_hash`` the delivered image's
content signature (the change detector that keeps the 60s refresher from
touching Discord), ``loot_updated_at`` the last delivery stamp (also the
mtime watermark that skips re-reading an unchanged PNG).

Revision ID: web93a_team_loot_posts
Revises: web92a_boost_target_ids
"""
from alembic import op
import sqlalchemy as sa

revision = "web93a_team_loot_posts"
down_revision = "web92a_boost_target_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("web_event_team_discord",
                  sa.Column("loot_message_id", sa.String(32), nullable=True))
    op.add_column("web_event_team_discord",
                  sa.Column("loot_state_hash", sa.String(64), nullable=True))
    op.add_column("web_event_team_discord",
                  sa.Column("loot_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("web_event_team_discord", "loot_updated_at")
    op.drop_column("web_event_team_discord", "loot_state_hash")
    op.drop_column("web_event_team_discord", "loot_message_id")
