"""precise_timing on the seasonal + hardcore personal-best mirrors (web157a).

web127a added ``personal_best.precise_timing`` and the PB processor writes it
for every world, but the mirror tables never got the column. The first
Hardcore Worlds PB per (player, boss) built its row with it and the ORM
refused ("'precise_timing' is an invalid keyword argument for
HardcorePersonalBestEntry"), dead-lettering the submission. Seasonal had the
same gap, unseen while no league is running. Nullable, no backfill: NULL is
already "unknown" (utils.pb_time).

Revision ID: web157a_mirror_pb_precise_timing
Revises: web156a_event_partial_points
"""
from alembic import op
import sqlalchemy as sa

revision = "web157a_mirror_pb_precise_timing"
down_revision = "web156a_event_partial_points"
branch_labels = None
depends_on = None

_TABLES = ("seasonal_personal_best", "hardcore_personal_best")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in _TABLES:
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "precise_timing" not in columns:
            op.add_column(table, sa.Column("precise_timing", sa.Boolean(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in _TABLES:
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "precise_timing" in columns:
            op.drop_column(table, "precise_timing")
