"""Internal dev tracker tables (web81a).

Four new tables backing the owner's project/task board (superadmin CMS at
/admin/projects + scripts/project_tracker.py for codebase agents):

    dev_projects  -> dev_tasks -> dev_subtasks
                 \\-> dev_notes (project- or task-scoped)

Purely additive — touches nothing existing. Child FKs cascade on delete so
removing a project (or task) takes its subtree with it. See
db/models/dev_tracker.py for the ORM definitions and field semantics.

Revision ID: web81a_dev_tracker
Revises: web80a_unique_id_indexes
"""
from alembic import op
import sqlalchemy as sa

revision = "web81a_dev_tracker"
down_revision = "web80a_unique_id_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dev_projects",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("completion_note", sa.Text(), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("author", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_dev_project_status_order", "dev_projects", ["status", "order"])

    op.create_table(
        "dev_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("dev_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="planned"),
        sa.Column("completion_note", sa.Text(), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("author", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_dev_task_project", "dev_tasks", ["project_id", "order"])

    op.create_table(
        "dev_subtasks",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("task_id", sa.Integer(),
                  sa.ForeignKey("dev_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_dev_subtask_task", "dev_subtasks", ["task_id", "order"])

    op.create_table(
        "dev_notes",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("dev_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Integer(),
                  sa.ForeignKey("dev_tasks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("author", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_dev_note_project", "dev_notes", ["project_id"])
    op.create_index("idx_dev_note_task", "dev_notes", ["task_id"])


def downgrade() -> None:
    op.drop_table("dev_notes")
    op.drop_table("dev_subtasks")
    op.drop_table("dev_tasks")
    op.drop_table("dev_projects")
