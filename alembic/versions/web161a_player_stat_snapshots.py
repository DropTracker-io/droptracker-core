"""player_stat_snapshots (web161a).

Our own history of a player's stats: skill XP now, hiscores activities and
EHP/EHB as the WOM-independence work lands (dev tracker project 40). One
store for every reader, replacing the ``player_xp_daily`` table this revision
was first drafted with (never applied anywhere).

A row is one observation of one player from one source. No foreign key: the
table is partitioned by month so the retention job can rebuild an old month
cheaply after downsampling it, and MariaDB does not allow foreign keys on a
partitioned table. ``pmax`` catches anything past the last named month; the
retention job splits new months out of it.

New table, no backfill: history starts at each player's first sync after
this lands.

Revision ID: web161a_player_stat_snapshots
Revises: web160a_event_guests
"""
from datetime import date

from alembic import op
import sqlalchemy as sa

revision = "web161a_player_stat_snapshots"
down_revision = "web160a_event_guests"
branch_labels = None
depends_on = None

TABLE = "player_stat_snapshots"
MONTHS_AHEAD = 13


def _month_partitions(start: date, count: int) -> str:
    parts = []
    year, month = start.year, start.month
    for _ in range(count):
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        parts.append(
            f"PARTITION p{year:04d}{month:02d} "
            f"VALUES LESS THAN ('{next_year:04d}-{next_month:02d}-01 00:00:00')"
        )
        year, month = next_year, next_month
    parts.append("PARTITION pmax VALUES LESS THAN (MAXVALUE)")
    return ",\n  ".join(parts)


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("player_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("taken_at", sa.DateTime(), primary_key=True),
        sa.Column("source", sa.String(10), primary_key=True),
        sa.Column("account_type", sa.SmallInteger(), nullable=True),
        sa.Column("total_xp", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_level", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("skills", sa.Text(), nullable=False),
        sa.Column("activities", sa.Text(), nullable=True),
        sa.Column("ranks", sa.Text(), nullable=True),
        sa.Column("ehp", sa.Float(), nullable=True),
        sa.Column("ehb", sa.Float(), nullable=True),
        sa.Column("flagged", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default="0"),
    )
    if bind.dialect.name in ("mysql", "mariadb"):
        op.execute(
            f"ALTER TABLE {TABLE} PARTITION BY RANGE COLUMNS (taken_at) (\n  "
            f"{_month_partitions(date.today().replace(day=1), MONTHS_AHEAD)}\n)"
        )


def downgrade() -> None:
    op.drop_table(TABLE)
