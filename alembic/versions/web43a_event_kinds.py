"""Event kinds (game formats) + the site-wide event-type registry.

Adds ``web_events.kind`` (standard|bingo|board_game — orthogonal to ``mode``,
which is ownership shape) and two new tables:

- ``web_event_types``            — one row per kind: enabled / admin_only
                                   toggles managed from /admin/event-types.
- ``web_event_type_test_groups`` — per-kind allowlist; admins of a listed
                                   group may create the kind even while it is
                                   disabled/admin_only (beta-test mechanism).

The gate binds at CREATION only (services/event_types.py) — existing events
of a toggled-off kind keep running.

Backfill: events with has_bingo=1 become kind='bingo'; everything else stays
'standard'. board_game is seeded enabled but admin_only (ships dark).

Revision ID: web43a_event_kinds
Revises: data01_drops_player_date_idx
"""
from alembic import op
import sqlalchemy as sa

revision = "web43a_event_kinds"
down_revision = "data01_drops_player_date_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("kind", sa.String(24), nullable=False, server_default="standard"),
    )
    op.execute("UPDATE web_events SET kind = 'bingo' WHERE has_bingo = 1")

    op.create_table(
        "web_event_types",
        sa.Column("key", sa.String(24), primary_key=True),
        sa.Column("label", sa.String(48), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("admin_only", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "web_event_type_test_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "type_key", sa.String(24), sa.ForeignKey("web_event_types.key"), nullable=False
        ),
        sa.Column(
            "group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=False
        ),
        sa.Column(
            "added_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_web_evt_type_test_group",
        "web_event_type_test_groups",
        ["type_key", "group_id"],
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
                "key": "standard",
                "label": "Standard",
                "description": "A flat task list scored by completions.",
                "enabled": True,
                "admin_only": False,
                "sort": 0,
            },
            {
                "key": "bingo",
                "label": "Bingo",
                "description": "A square task grid with line and blackout bonuses.",
                "enabled": True,
                "admin_only": False,
                "sort": 1,
            },
            {
                "key": "board_game",
                "label": "Board game",
                "description": (
                    "A dice board: teams complete their tile's task, roll to "
                    "advance, earn coins and spend them on power-ups. "
                    "Currently in staff testing."
                ),
                "enabled": True,
                "admin_only": True,
                "sort": 2,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("uq_web_evt_type_test_group", table_name="web_event_type_test_groups")
    op.drop_table("web_event_type_test_groups")
    op.drop_table("web_event_types")
    op.drop_column("web_events", "kind")
