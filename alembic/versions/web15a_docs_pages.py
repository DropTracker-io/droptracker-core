"""Docs CMS (backend Task 15/16) — user-editable docs pages.

Replaces the static `.mdx` files under `apps/web/content/docs/` with a DB
table so non-technical staff can add/edit/delete documentation from
`/admin/docs` without a code deploy.
"""

from alembic import op
import sqlalchemy as sa


revision = "web15a_docs_pages"
down_revision = "web14a_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "docs_pages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=False, server_default="General"),
        sa.Column("order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uix_docs_page_slug"),
    )
    op.create_index("idx_docs_page_category_order", "docs_pages", ["category", "order"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_docs_page_category_order", table_name="docs_pages")
    op.drop_table("docs_pages")
