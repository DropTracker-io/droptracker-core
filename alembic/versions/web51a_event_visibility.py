"""Event-level visibility (public / private).

- ``web_events.visibility`` — "public" (default; anyone can see the event) or
  "private" (only participating-group members + event admins ever see it, at
  any lifecycle status). Powers the "keep this event private" toggle and the
  read gate in ``web_api/routes/events.py``.

Revision ID: web51a_event_visibility
Revises: web50a_boardgame_shop_expansion
"""
from alembic import op
import sqlalchemy as sa

revision = "web51a_event_visibility"
down_revision = "web50a_boardgame_shop_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column(
            "visibility",
            sa.String(16),
            nullable=False,
            server_default="public",
        ),
    )


def downgrade() -> None:
    op.drop_column("web_events", "visibility")
