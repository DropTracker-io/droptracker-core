"""web_event_effort.completions — attempts that reached the WOM-counted event

At most NPCs a kill is a kill, and this column stays 0. At the ones where the
content pays out for a partial attempt — the Fortis Colosseum is the case that
forced this — the plugin's loot KC counts attempts while WOM's boss metric only
counts completions, and pricing an attempt at the completion rate charged 22
minutes for a run our own timings put at about two. Counting the two separately
is the only way to price either honestly.

See services/event_effort.COMPLETION_MARKERS.

Revision ID: web104a_effort_completions
Revises: dapi3_key_scope
"""
from alembic import op
import sqlalchemy as sa

revision = "web104a_effort_completions"
down_revision = "dapi3_key_scope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "web_event_effort",
        sa.Column("completions", sa.BigInteger(), nullable=False,
                  server_default="0"),
    )


def downgrade():
    op.drop_column("web_event_effort", "completions")
