"""Per-page custom CSS for group mini-sites.

Sites already carry one site-wide stylesheet; a page that needs its own
layout (a bespoke "about" page, say) had to put those rules in the global
sheet where they applied everywhere. These columns mirror the site-level
pair exactly: *_source is what the editor round-trips, custom_css is the
tinycss2-validated + #site-root-scoped output and the only column rendered.

Revision ID: web90a_site_page_css
Revises: web89a_group_sites
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

revision = "web90a_site_page_css"
down_revision = "web89a_group_sites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_site_pages", sa.Column("custom_css", MEDIUMTEXT(), nullable=True))
    op.add_column(
        "group_site_pages", sa.Column("custom_css_source", MEDIUMTEXT(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("group_site_pages", "custom_css_source")
    op.drop_column("group_site_pages", "custom_css")
