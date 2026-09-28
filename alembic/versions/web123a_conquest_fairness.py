"""Conquest fairness rules (web123a).

Retreat, safe capitals, comeback shields/boosts, underdog defense, leader
bounties, a contested centre, hot zones and phases. Adds:

``web_conquest_maps.phase_announced``  the last phase start posted to Discord
``web_conquest_regions.contested``     1 = the contested centre
``web_conquest_tiles.siege``           troops thrown at it this siege
``web_conquest_rules.phase``           0 = every phase, N = phase N only
``web_conquest_battles.points``        bonus points a troop won (bounties)
``web_conquest_hotzones``              regions that are hot for a while
``web_conquest_team_state``            per-team comeback bookkeeping

ADD COLUMNs (nullable, or NOT NULL with a default) on small tables and two
new tables, so it is safe to apply while events are running. Each step is
guarded, so a re-run picks up where it stopped.

Revision ID: web123a_conquest_fairness
Revises: web122a_conquest_fronts
"""
from alembic import op
import sqlalchemy as sa

revision = "web123a_conquest_fairness"
down_revision = "web122a_conquest_fronts"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("web_conquest_maps", sa.Column("phase_announced", sa.Integer(), nullable=True)),
    ("web_conquest_regions", sa.Column("contested", sa.Integer(), nullable=False,
                                       server_default="0")),
    ("web_conquest_tiles", sa.Column("siege", sa.Integer(), nullable=False,
                                     server_default="0")),
    ("web_conquest_rules", sa.Column("phase", sa.Integer(), nullable=False,
                                     server_default="0")),
    ("web_conquest_battles", sa.Column("points", sa.Float(), nullable=False,
                                       server_default="0")),
)


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _has_table(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # Fail fast instead of queueing behind a long metadata lock.
        bind.execute(sa.text("SET SESSION lock_wait_timeout = 30"))
    for table, column in _COLUMNS:
        if not _has_column(bind, table, column.name):
            op.add_column(table, column)
    if not _has_table(bind, "web_conquest_hotzones"):
        op.create_table(
            "web_conquest_hotzones",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(),
                      sa.ForeignKey("web_events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("region_id", sa.Integer(),
                      sa.ForeignKey("web_conquest_regions.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("ends_at", sa.DateTime(), nullable=False),
            sa.Column("announced_at", sa.DateTime(), nullable=True),
        )
        op.create_index("idx_web_conquest_hotzone_event", "web_conquest_hotzones",
                        ["event_id", "starts_at"])
    if not _has_table(bind, "web_conquest_team_state"):
        op.create_table(
            "web_conquest_team_state",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.Integer(),
                      sa.ForeignKey("web_events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("team_id", sa.Integer(), nullable=False),
            sa.Column("landless_since", sa.DateTime(), nullable=True),
            sa.Column("shield_until", sa.DateTime(), nullable=True),
            sa.Column("boost_until", sa.DateTime(), nullable=True),
            sa.Column("comebacks", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("uq_web_conquest_team_state", "web_conquest_team_state",
                        ["event_id", "team_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("web_conquest_team_state", "web_conquest_hotzones"):
        if _has_table(bind, table):
            op.drop_table(table)
    for table, column in reversed(_COLUMNS):
        if _has_column(bind, table, column.name):
            op.drop_column(table, column.name)
