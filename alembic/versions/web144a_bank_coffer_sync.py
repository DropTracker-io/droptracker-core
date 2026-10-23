"""group_bank_entries.external_ref (web144a): in-game coffer sync.

Rows written from relayed clan coffer broadcasts (``source='coffer'``) carry a
deterministic reference built from the clan, the broadcast line and its time
bucket (``services/group_bank.coffer_ref``). The unique (group_id,
external_ref) index is what makes the sync idempotent: however many clanmates
relay the same line, and however often the queue redelivers it, it books
once. Staff and member rows leave it NULL, which the index ignores.

Adds a nullable column and an index on a small table, so it is safe to apply
live. Guarded, so a re-run is harmless.

Revision ID: web144a_bank_coffer_sync
Revises: web143a_plugin_admin_sessions
"""
from alembic import op
import sqlalchemy as sa

revision = "web144a_bank_coffer_sync"
down_revision = "web143a_plugin_admin_sessions"
branch_labels = None
depends_on = None

_INDEX = "uq_gbank_group_external_ref"


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("group_bank_entries")}
    if "external_ref" not in columns:
        op.add_column(
            "group_bank_entries", sa.Column("external_ref", sa.String(80), nullable=True)
        )
    indexes = {i["name"] for i in inspector.get_indexes("group_bank_entries")}
    if _INDEX not in indexes:
        op.create_index(
            _INDEX, "group_bank_entries", ["group_id", "external_ref"], unique=True
        )


def downgrade():
    op.drop_index(_INDEX, table_name="group_bank_entries")
    op.drop_column("group_bank_entries", "external_ref")
