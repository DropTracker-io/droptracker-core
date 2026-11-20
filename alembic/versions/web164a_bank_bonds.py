"""group_bank_entries.asset (web164a).

Clan bank bonds (suggestion #253): clans pay event prizes in Old School bonds
and want to see how many they hold. Bonds are their own balance, never
converted into GP, so a row says what it counts: ``asset`` is ``gp`` (every
existing row, via the server default; no backfill) or ``bond``, whose
``amount`` is a signed number of bonds. See services/group_bank.py.

Revision ID: web164a_bank_bonds
Revises: web163a_api_key_requests
"""
from alembic import op
import sqlalchemy as sa

revision = "web164a_bank_bonds"
down_revision = "web163a_api_key_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("group_bank_entries")}
    if "asset" not in columns:
        op.add_column(
            "group_bank_entries",
            sa.Column("asset", sa.String(8), nullable=False, server_default="gp"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("group_bank_entries")}
    if "asset" not in columns:
        return
    # Without the column a bond row would read as that many GP. Refuse rather
    # than silently corrupt GP balances or delete a clan's bond history.
    bonds = bind.execute(sa.text(
        "SELECT COUNT(*) FROM group_bank_entries WHERE asset <> 'gp'"
    )).scalar()
    if bonds:
        raise RuntimeError(
            f"{bonds} clan bank bond row(s) exist; remove or export them before "
            "downgrading web164a, or they would be counted as GP."
        )
    op.drop_column("group_bank_entries", "asset")
