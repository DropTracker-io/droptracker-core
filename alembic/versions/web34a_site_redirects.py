"""Admin-configurable redirects: site_redirects table.

Backs the `/admin/redirects` CMS + the front-end middleware that resolves
redirects at request time (no code deploy to add/change one). See
db/models/web.py::SiteRedirect and web_api/routes/redirects.py.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "web34a_site_redirects"
down_revision = "web33a_task_library_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_redirects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=512), nullable=False),
        sa.Column("destination", sa.String(length=1024), nullable=False),
        sa.Column("permanent", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("order", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("forward_query", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", name="uq_site_redirects_source"),
    )
    op.create_index("idx_site_redirect_order", "site_redirects", ["order", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_site_redirect_order", table_name="site_redirects")
    op.drop_table("site_redirects")
