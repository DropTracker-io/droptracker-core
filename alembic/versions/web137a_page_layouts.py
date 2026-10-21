"""page_layouts: customizable group pages, player profiles and member defaults (web137a).

One row per (scope, subject). scope is "group" (subject = group_id),
"player" (subject = player_id) or "member_default" (subject = group_id: the
layout a group's members get unless they publish their own). Draft and
published are separate columns, copy-on-publish, like group_site_pages. See
db/models/page_layout.py.

Revision ID: web137a_page_layouts
Revises: data02_drops_index_cleanup
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "web137a_page_layouts"
down_revision = "data02_drops_index_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_layouts",
        sa.Column("layout_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("draft_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("published_json", mysql.LONGTEXT(), nullable=True),
        sa.Column(
            "updated_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("scope", "subject_id", name="uq_page_layouts_scope_subject"),
    )


def downgrade() -> None:
    op.drop_table("page_layouts")
