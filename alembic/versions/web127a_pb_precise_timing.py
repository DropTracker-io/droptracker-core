"""personal_best.precise_timing (web127a).

Whether the time on the row was printed by a client with precise timing on.
A client with it off prints whole seconds, and the server can only credit
such a time with the slowest tick it could stand for (``utils.pb_time``).
Recording which kind a row holds lets boards mark those times as approximate
and settle a tie in favour of the time that was actually measured.

NULL means unknown: rows written before this column, and paths that never
saw the formatted time (raw-ms form submissions).

Adding a nullable column is an instant ALTER on MariaDB, so this is safe to
apply live. Guarded, so a re-run is harmless.

Revision ID: web127a_pb_precise_timing
Revises: web126a_tester_builds
"""
from alembic import op
import sqlalchemy as sa

revision = "web127a_pb_precise_timing"
down_revision = "web126a_tester_builds"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("personal_best")}
    if "precise_timing" not in columns:
        op.add_column(
            "personal_best",
            sa.Column("precise_timing", sa.Boolean(), nullable=True),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("personal_best")}
    if "precise_timing" in columns:
        op.drop_column("personal_best", "precise_timing")
