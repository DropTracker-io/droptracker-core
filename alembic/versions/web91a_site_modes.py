"""Site modes: builder | group_page | redirect.

Not every clan wants to maintain a site — plenty just want the address. A
claimed subdomain can now either render the block builder (as before), bounce
to the group's DropTracker profile, or bounce to a URL the group supplies
(their Discord, a forum, whatever).

Revision ID: web91a_site_modes
Revises: web90a_site_page_css
"""
from alembic import op
import sqlalchemy as sa

revision = "web91a_site_modes"
down_revision = "web90a_site_page_css"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_sites",
        sa.Column("mode", sa.String(16), nullable=False, server_default="builder"),
    )
    op.add_column("group_sites", sa.Column("redirect_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("group_sites", "redirect_url")
    op.drop_column("group_sites", "mode")
