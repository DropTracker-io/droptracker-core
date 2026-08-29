"""SOTW/BOTW competition events — WOM linkage table + kind registry rows

Two new event kinds, ``sotw`` and ``botw``: individuals race XP gained in one
skill / KC gained at one boss, hosted purely on DropTracker or mirroring /
creating a WiseOldMan competition. The scoring config lives in the hidden
``competition`` task's config JSON (the loot_sweep philosophy); this table
carries only what has no existing home — the WOM linkage + verification code
(SECRET, created mode), the poller's sync bookkeeping (kept off ``web_events``
so 5-minute sync stamps don't churn that row), the cached standings of WOM
participants with no DropTracker account, and the frozen final standings.

The registry rows ship dark (enabled=0, admin_only=1) — the loot_sweep launch
recipe: staff + web_event_type_test_groups clans see the kinds first, the
enabled flag flips at GA.

Revision ID: web105a_event_competitions
Revises: web100a_player_name_norm_index
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "web105a_event_competitions"
# NOTE the nonlinear numbering: web100a_player_name_norm_index is the newest
# revision (it postdates web104a — the number was reused), so the chain tip
# this extends is web100a, not web104a. tests/unit/test_alembic_single_head.py
# is the referee.
down_revision = "web100a_player_name_norm_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "web_event_competitions",
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("web_events.id"), primary_key=True
        ),
        sa.Column("source_mode", sa.String(16), nullable=False, server_default="hosted"),
        sa.Column("wom_competition_id", sa.Integer(), nullable=True),
        sa.Column("wom_competition_code", sa.String(64), nullable=True),
        sa.Column("wom_title", sa.String(128), nullable=True),
        sa.Column("wom_starts_at", sa.DateTime(), nullable=True),
        sa.Column("wom_ends_at", sa.DateTime(), nullable=True),
        sa.Column("wom_synced_at", sa.DateTime(), nullable=True),
        sa.Column("wom_sync_error", sa.String(255), nullable=True),
        sa.Column(
            "wom_standings",
            sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=True,
        ),
        sa.Column(
            "final_standings",
            sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=True,
        ),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    # "This WOM competition is already linked to event #N." MySQL unique
    # indexes admit unlimited NULLs, so hosted rows (wom_competition_id NULL)
    # never collide.
    op.create_index(
        "uq_web_evt_comp_womid",
        "web_event_competitions",
        ["wom_competition_id"],
        unique=True,
    )

    types = sa.table(
        "web_event_types",
        sa.column("key", sa.String),
        sa.column("label", sa.String),
        sa.column("description", sa.Text),
        sa.column("enabled", sa.Boolean),
        sa.column("admin_only", sa.Boolean),
        sa.column("sort", sa.Integer),
    )
    op.bulk_insert(
        types,
        [
            {
                "key": "sotw",
                "label": "Skill of the Week",
                "description": (
                    "Race to gain the most XP in one skill before the event "
                    "ends — runs for any length you choose."
                ),
                "enabled": False,
                "admin_only": True,
                "sort": 6,
            },
            {
                "key": "botw",
                "label": "Boss of the Week",
                "description": (
                    "Race the most kills at one boss — with bonus points for "
                    "pets and fast kill times."
                ),
                "enabled": False,
                "admin_only": True,
                "sort": 7,
            },
        ],
    )


def downgrade():
    op.execute("DELETE FROM web_event_types WHERE `key` IN ('sotw', 'botw')")
    op.drop_index("uq_web_evt_comp_womid", table_name="web_event_competitions")
    op.drop_table("web_event_competitions")
