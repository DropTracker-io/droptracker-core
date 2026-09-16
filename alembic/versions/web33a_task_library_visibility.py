"""Task-library sharing: public/private task saves per group.

- `web_event_tasks.visibility` ("public" | "private", server_default "public"):
  the publicity an admin picked when creating/editing the task. Existing tasks
  read back as public (the announced default for pre-existing tasks).
- `web_event_task_library.group_id` (nullable FK): owning group for
  group-saved rows; NULL = site-wide (curated legacy_v1 seeds, or rows saved
  from global events by superadmins).
- `web_event_task_library.visibility` (server_default "public"): public rows
  appear in every group's picker, private rows only in the owning group's.
- Unique index widens from (name, source) to (name, source, group_id) so two
  groups can privately save same-named tasks without colliding.
- Backfill: every pre-existing event task is copied into the library as a
  PUBLIC group-saved row (source='group', owned by its event's group), deduped
  case-insensitively by name against both the library and itself, with the
  bingo designer's `bingo_auto` config marker stripped.
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "web33a_task_library_visibility"
down_revision = "web32a_clan_vs_clan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_tasks",
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="public"),
    )
    op.add_column(
        "web_event_task_library",
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True),
    )
    op.add_column(
        "web_event_task_library",
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="public"),
    )
    op.drop_index("uq_web_evt_library_name_source", table_name="web_event_task_library")
    op.create_index(
        "uq_web_evt_library_name_source_group",
        "web_event_task_library",
        ["name", "source", "group_id"],
        unique=True,
    )
    op.create_index("idx_web_evt_library_group", "web_event_task_library", ["group_id"])

    # Backfill: existing event tasks -> public library rows.
    bind = op.get_bind()
    taken = {
        name.lower()
        for (name,) in bind.execute(sa.text("SELECT name FROM web_event_task_library"))
    }
    rows = bind.execute(sa.text(
        "SELECT t.label, t.type, t.target, t.target_value, t.points, t.config, e.group_id "
        "FROM web_event_tasks t JOIN web_events e ON e.id = t.event_id ORDER BY t.id"
    ))
    for label, ttype, target, target_value, points, config, group_id in rows:
        name = (label or "").strip()[:120]
        if not name or name.lower() in taken:
            continue
        taken.add(name.lower())
        if config:
            # Drop the designer's auto-created marker; it's task-instance
            # bookkeeping, not part of the reusable preset.
            try:
                parsed = json.loads(config)
                if isinstance(parsed, dict) and parsed.pop("bingo_auto", None) is not None:
                    config = json.dumps(parsed) if parsed else None
            except ValueError:
                pass
        bind.execute(
            sa.text(
                "INSERT INTO web_event_task_library "
                "(name, description, type, target, target_value, default_points, "
                " difficulty, config, source, group_id, visibility, active) "
                "VALUES (:name, NULL, :type, :target, :target_value, :points, "
                " NULL, :config, 'group', :group_id, 'public', 1)"
            ),
            {
                "name": name,
                "type": ttype,
                "target": target,
                "target_value": target_value,
                "points": int(points or 0),
                "config": config,
                "group_id": group_id,
            },
        )


def downgrade() -> None:
    op.execute("DELETE FROM web_event_task_library WHERE source = 'group'")
    op.drop_index("idx_web_evt_library_group", table_name="web_event_task_library")
    op.drop_index("uq_web_evt_library_name_source_group", table_name="web_event_task_library")
    op.create_index(
        "uq_web_evt_library_name_source",
        "web_event_task_library",
        ["name", "source"],
        unique=True,
    )
    op.drop_column("web_event_task_library", "visibility")
    op.drop_column("web_event_task_library", "group_id")
    op.drop_column("web_event_tasks", "visibility")
