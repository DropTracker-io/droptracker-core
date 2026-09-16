"""players.ehb — WOM efficient hours bossed (web60a).

WOM player-detail responses already carry a top-level ``ehb`` figure on every
identity fetch we make (submission intake, create_player), but we only stored
total_level / log_slots. Persist EHB alongside them so it refreshes for free
wherever WOM data is already flowing. NULL = never fetched (distinct from a
genuine 0.0 for a bossless account).

Revision ID: web60a_player_ehb
Revises: web59a_member_event_unique
"""
from alembic import op
import sqlalchemy as sa

revision = "web60a_player_ehb"
down_revision = "web59a_member_event_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("ehb", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "ehb")
