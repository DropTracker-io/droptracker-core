"""Which Discord channel a notice posts to (web133a).

HQ now has two update channels: #news for important changes and #updates for
the smaller ones (fixes, tweaks, new options), which replaced the separate
plugin / website / discord update channels. ``discord_target`` says which one
a notice with ``post_to_discord`` goes to: ``news`` (the default, and the
only choice that needs the news post) or ``updates``.

Additive and nullable (NULL reads as ``news``), so it is safe to apply live.
Guarded, so a re-run is harmless.

Revision ID: web133a_notice_discord_target
Revises: web132a_player_last_seen
"""
from alembic import op
import sqlalchemy as sa

revision = "web133a_notice_discord_target"
down_revision = "web132a_player_last_seen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("popup_notices")}
    if "discord_target" not in existing:
        op.add_column("popup_notices", sa.Column("discord_target", sa.String(12), nullable=True))


def downgrade() -> None:
    op.drop_column("popup_notices", "discord_target")
