"""Per-event EHE visibility (web74a).

``web_events.effort_visibility``:

* ``public`` (default) — a player's Efficient Hours towards Event shows on the
  team page, the Players tab and the event-detail rosters, as it does today.
* ``admins`` — the figure is confined to the event managers' effort report.

Effort is always RECORDED either way; only the display is gated. Some clans
don't want a per-member effort number on a public page, because it reads as
"here is who did the least" — which is a social problem, not a data one.

Revision ID: web74a_effort_visibility
Revises: web73a_npc_ehb_rates
"""
from alembic import op
import sqlalchemy as sa

revision = "web74a_effort_visibility"
down_revision = "web73a_npc_ehb_rates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("effort_visibility", sa.String(16), nullable=False,
                  server_default="public"),
    )


def downgrade() -> None:
    op.drop_column("web_events", "effort_visibility")
