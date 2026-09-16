"""Persist the kill count that already arrives with every drop (web76a).

The RuneLite plugin sends a kill count on every drop submission; ``/webhook``
forwards it, ``data/submissions/drop.py`` parses it, and it reaches the Discord
embed as ``{kill_count}`` and the events KC-dedupe set — and was then thrown
away. Roughly 63% of notified drops carry a non-null KC, so this is a large
signal we were discarding at source and could never recover retroactively.

Storing it unlocks KC-at-drop history: dry streaks, "you got X at N KC", luck
percentiles, and eventually DropTracker's own empirically observed drop rates.

``drops`` is ~31 GB / ~175M rows, so a rebuilding ALTER would lock intake for
hours. ``ALGORITHM=INSTANT`` is stated explicitly rather than left to the
optimizer: appending a nullable column qualifies on MariaDB 10.3+, and naming it
means the statement fails fast if it ever stops qualifying instead of silently
rewriting the table.

No backfill is possible — the value only exists in flight.

Revision ID: web76a_drop_kill_count
Revises: 1db475d07a79
"""
from alembic import op
import sqlalchemy as sa

revision = "web76a_drop_kill_count"
down_revision = "1db475d07a79"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE drops ADD COLUMN kill_count INT NULL, ALGORITHM=INSTANT")


def downgrade() -> None:
    # DROP COLUMN cannot be INSTANT on MariaDB; this rebuilds the table and is
    # only realistic during a maintenance window.
    op.drop_column("drops", "kill_count")
