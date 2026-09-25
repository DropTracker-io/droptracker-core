"""Conquest territory shapes (web121a).

The Gielinor preset now draws the real world map: every tile owns a
territory (an SVG path) and every region an outline, laid over a terrain
backdrop the website serves. Adds:

``web_conquest_tiles.shape``        the territory outline (NULL = a plain badge)
``web_conquest_regions.shape``      the region outline
``web_conquest_maps.shape_width``   the coordinate space the shapes are in,
``web_conquest_maps.shape_height``  independent of the background image

Nullable ADD COLUMNs on three small tables, so it is safe to apply while
events are running. Each step is guarded, so a re-run picks up where it
stopped.

Revision ID: web121a_conquest_shapes
Revises: web120a_conquest
"""
from alembic import op
import sqlalchemy as sa

revision = "web121a_conquest_shapes"
down_revision = "web120a_conquest"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("web_conquest_tiles", "shape", sa.Text),
    ("web_conquest_regions", "shape", sa.Text),
    ("web_conquest_maps", "shape_width", sa.Integer),
    ("web_conquest_maps", "shape_height", sa.Integer),
)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # Fail fast instead of queueing behind a long metadata lock.
        bind.execute(sa.text("SET SESSION lock_wait_timeout = 30"))
    for table, column, kind in _COLUMNS:
        if not _has_column(bind, table, column):
            op.add_column(table, sa.Column(column, kind(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    for table, column, _kind in reversed(_COLUMNS):
        if _has_column(bind, table, column):
            op.drop_column(table, column)
