"""group_bank_entries.event_label (web158a).

Clan bank transfers to events run outside DropTracker (suggestion #160
follow-up): a clan names the event ("Weekly PvM night") and the GP is booked
as an ``event_transfer`` with that label, no ``event_id`` and no prize-pot row.
Nullable, no backfill: every existing transfer has an ``event_id``.

Revision ID: web158a_bank_outside_events
Revises: web157a_mirror_pb_precise_timing
"""
from alembic import op
import sqlalchemy as sa

revision = "web158a_bank_outside_events"
down_revision = "web157a_mirror_pb_precise_timing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("group_bank_entries")}
    if "event_label" not in columns:
        op.add_column("group_bank_entries", sa.Column("event_label", sa.String(64), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("group_bank_entries")}
    if "event_label" in columns:
        op.drop_column("group_bank_entries", "event_label")
