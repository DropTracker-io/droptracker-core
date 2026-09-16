"""Per-event message-layout overrides (web66a).

web_event_message_layouts gains an ``event_id`` dimension: 0 (the default)
keeps the row a group-level layout, a real event id makes it a one-event
override resolved ahead of the group's default by
services/event_message_layouts.load_layout (event -> group -> template
group 1 -> code default).

``event_id`` is NOT NULL with a 0 sentinel rather than nullable on purpose:
MySQL unique indexes admit unlimited NULL duplicates, so a nullable column
would let concurrent saves double-write a group's layout (the same failure
mode web56a fixed for users.discord_id). No FK to web_events for the same
reason — 0 references no row. Orphan cleanup rides event deletion
(web_api/routes/events.py _cascade_delete_event).

The widened unique index gets a _v2 name and is created BEFORE the old one
is dropped: the group_id FK needs a supporting index at all times (MariaDB
error 1553 otherwise), and the replacement leads with group_id so it takes
over that duty. Steps are guarded so a partially-applied run (the first
deploy attempt added the column, then failed on the index drop) re-runs
clean.

Revision ID: web66a_event_layout_overrides
Revises: web65a_event_rate_limits
"""
from alembic import op
import sqlalchemy as sa

revision = "web66a_event_layout_overrides"
down_revision = "web65a_event_rate_limits"
branch_labels = None
depends_on = None

_TABLE = "web_event_message_layouts"


def _state(conn):
    insp = sa.inspect(conn)
    cols = {c["name"] for c in insp.get_columns(_TABLE)}
    idx = {i["name"] for i in insp.get_indexes(_TABLE)}
    return cols, idx


def upgrade() -> None:
    conn = op.get_bind()
    cols, idx = _state(conn)
    if "event_id" not in cols:
        op.add_column(
            _TABLE, sa.Column("event_id", sa.Integer(), nullable=False, server_default="0")
        )
    if "uq_web_event_msg_layout_v2" not in idx:
        op.create_index(
            "uq_web_event_msg_layout_v2",
            _TABLE,
            ["group_id", "message_type", "event_id"],
            unique=True,
        )
    if "uq_web_event_msg_layout" in idx:
        op.drop_index("uq_web_event_msg_layout", table_name=_TABLE)


def downgrade() -> None:
    conn = op.get_bind()
    cols, idx = _state(conn)
    op.execute(f"DELETE FROM {_TABLE} WHERE event_id != 0")
    if "uq_web_event_msg_layout" not in idx:
        op.create_index(
            "uq_web_event_msg_layout", _TABLE, ["group_id", "message_type"], unique=True
        )
    if "uq_web_event_msg_layout_v2" in idx:
        op.drop_index("uq_web_event_msg_layout_v2", table_name=_TABLE)
    if "event_id" in cols:
        op.drop_column(_TABLE, "event_id")
