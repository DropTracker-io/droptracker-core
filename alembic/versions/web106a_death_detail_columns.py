"""Record what a death actually was, not just where it happened (web106a).

The plugin has been sending the killer's type, the PvP flag, a resolved area
name and a safe/dangerous verdict since 6.0, and the value of the items lost
since 6.0.4. ``death_processor`` read four fields and dropped the rest on the
floor, so none of it reached the row or the notification payload — which is
what a group's safe-death filter and its region blacklist have to read.

All columns are nullable, and NULL genuinely means "unknown": a submission from
a pre-6.0 client, or one the client could not locate. That is deliberately
distinct from ``False`` / ``0``. ``is_safe_death`` NULL is not "this death was
dangerous", and ``value_lost`` NULL is not "died carrying nothing" — collapsing
either would make an old client's death indistinguishable from a real cheap one.

``value_lost`` is BIGINT: a max-cash-stack death overflows a signed INT.

Revision ID: web106a_death_detail_columns
Revises: web105a_event_competitions
"""
from alembic import op
import sqlalchemy as sa

revision = "web106a_death_detail_columns"
down_revision = "web105a_event_competitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("player_deaths", sa.Column("region_name", sa.String(length=125), nullable=True))
    op.add_column("player_deaths", sa.Column("killer_type", sa.String(length=16), nullable=True))
    op.add_column("player_deaths", sa.Column("is_pvp", sa.Boolean(), nullable=True))
    op.add_column("player_deaths", sa.Column("is_safe_death", sa.Boolean(), nullable=True))
    op.add_column("player_deaths", sa.Column("value_lost", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("player_deaths", "value_lost")
    op.drop_column("player_deaths", "is_safe_death")
    op.drop_column("player_deaths", "is_pvp")
    op.drop_column("player_deaths", "killer_type")
    op.drop_column("player_deaths", "region_name")
