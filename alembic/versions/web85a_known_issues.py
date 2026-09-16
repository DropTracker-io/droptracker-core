"""Known-issues board tables (web85a).

Backs the #status Discord channel's "known issues" card and the /admin/status
superadmin page: categories are section headers, issues the entries beneath.
Purely additive; no existing tables touched.

Revision ID: web85a_known_issues
Revises: web84a_unique_id_uniqueness
"""
from alembic import op
import sqlalchemy as sa

revision = "web85a_known_issues"
down_revision = "web84a_unique_id_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "known_issue_categories",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("emoji", sa.String(32), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("idx_known_issue_cat_order", "known_issue_categories", ["order", "id"])

    op.create_table(
        "known_issues",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("category_id", sa.Integer(),
                  sa.ForeignKey("known_issue_categories.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="minor"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_by", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_known_issue_category", "known_issues", ["category_id", "order"])
    op.create_index("idx_known_issue_status", "known_issues", ["status"])


def downgrade() -> None:
    op.drop_table("known_issues")
    op.drop_table("known_issue_categories")
