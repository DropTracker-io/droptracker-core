"""Conquest fronts and organiser overrides (web122a).

Troops now only count on tiles a team can reach (its own tiles and the ones
bordering them), organisers can pick each team's home tile, and more of the
game is set per tile. Adds:

``web_conquest_tiles.max_defense``   this tile's defense cap (NULL = the map's)
``web_conquest_tiles.garrison``      its defense while unowned (NULL = the map's)
``web_conquest_tiles.home_team_id``  the team that starts here ("homes" start)
``web_conquest_rules.once``          1 = a one-time award
``web_conquest_troops.held``         troops earned out of reach

ADD COLUMNs on three small tables (nullable, or NOT NULL with a default), so
it is safe to apply while events are running. Each step is guarded, so a
re-run picks up where it stopped.

Revision ID: web122a_conquest_fronts
Revises: web121a_conquest_shapes
"""
from alembic import op
import sqlalchemy as sa

revision = "web122a_conquest_fronts"
down_revision = "web121a_conquest_shapes"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("web_conquest_tiles", sa.Column("max_defense", sa.Integer(), nullable=True)),
    ("web_conquest_tiles", sa.Column("garrison", sa.Integer(), nullable=True)),
    ("web_conquest_tiles", sa.Column("home_team_id", sa.Integer(), nullable=True)),
    ("web_conquest_rules", sa.Column("once", sa.Integer(), nullable=False,
                                     server_default="0")),
    ("web_conquest_troops", sa.Column("held", sa.Integer(), nullable=False,
                                      server_default="0")),
)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # Fail fast instead of queueing behind a long metadata lock.
        bind.execute(sa.text("SET SESSION lock_wait_timeout = 30"))
    for table, column in _COLUMNS:
        if not _has_column(bind, table, column.name):
            op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in reversed(_COLUMNS):
        if _has_column(bind, table, column.name):
            op.drop_column(table, column.name)
