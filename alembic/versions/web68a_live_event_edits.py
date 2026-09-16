"""web68a: live event edits — web_events.allow_live_edits toggle.

Opt-in per-event boolean letting event admins keep editing the bingo board
after activation (the one structural surface locked once an event starts).
Defaults OFF for every existing and new event; the wizard's "Joining & rules"
step and the event settings form expose it, and every flip is audited via the
event.settings.update diff.
"""

import sqlalchemy as sa
from alembic import op

revision = "web68a_live_event_edits"
down_revision = "web67a_ticket_inactivity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("allow_live_edits", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("web_events", "allow_live_edits")
