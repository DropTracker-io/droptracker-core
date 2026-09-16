"""web_event_templates: save/rerun events as reusable templates.

A template is a named snapshot of an event's *structure* — config, tasks,
bingo layout, team names — captured from any event (draft/active/past) and
re-instantiated later as a fresh draft. Runtime state (dates, rosters,
completions/progress, scores, join code, Discord config, clan-vs-clan
participants) is never captured; clan_vs_clan structure is saved but always
re-runs as a standard draft.

Sharing mirrors the task library (web_event_task_library): `source`
('group' | curated), owning `group_id` (NULL = site-wide, e.g. saved from a
global event), `visibility` ('public' = every clan's picker, 'private' =
owning group only) and `active` soft-delete. The snapshot itself lives in
`payload` (versioned JSON, MEDIUMTEXT — worst-case boards exceed TEXT's 64KB);
the queryable columns exist purely for the picker cards.

See services/event_templates.py + web_api/routes/event_templates.py.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "web36a_event_templates"
down_revision = "web35a_event_discord_prefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # Provenance only — the template must outlive its source event.
        sa.Column(
            "source_event_id",
            sa.Integer(),
            sa.ForeignKey("web_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True),
        sa.Column(
            "created_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True
        ),
        # EVENT_TASK_VISIBILITIES reused; templates default private (sharing
        # a whole event site-wide is an explicit choice, unlike single tasks).
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="private"),
        # EVENT_MODES of the source event (informational — instantiation
        # always produces a standard draft).
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="standard"),
        sa.Column("has_bingo", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("board_size", sa.Integer(), nullable=False, server_default="5"),
        # Denormalized picker-card counts (authoritative copies in payload).
        sa.Column("task_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("team_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("times_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_web_evt_tmpl_group", "web_event_templates", ["group_id", "active"])
    op.create_index(
        "idx_web_evt_tmpl_visibility", "web_event_templates", ["visibility", "active"]
    )


def downgrade() -> None:
    op.drop_index("idx_web_evt_tmpl_visibility", table_name="web_event_templates")
    op.drop_index("idx_web_evt_tmpl_group", table_name="web_event_templates")
    op.drop_table("web_event_templates")
