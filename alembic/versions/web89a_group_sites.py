"""Group mini-sites (sites-v1): group_sites + group_site_pages.

Premium groups claim a unique subdomain on the tenant sites domain
(SITES_DOMAIN, working name osrs.site) and build a multi-page mini-site from
blocks. Draft/published are column pairs (copy-on-publish); the public
renderer reads only published columns behind the custom_site entitlement
render gate. Additive only.

Revision ID: web89a_group_sites
Revises: web88a_group_embed_url
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT, MEDIUMTEXT

revision = "web89a_group_sites"
down_revision = "web88a_group_embed_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_sites",
        sa.Column("site_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=False
        ),
        sa.Column("subdomain", sa.String(32), nullable=False),
        sa.Column("theme_key", sa.String(32), nullable=False, server_default="dusk"),
        sa.Column("palette", LONGTEXT(), nullable=True),
        sa.Column("nav", LONGTEXT(), nullable=True),
        sa.Column("custom_css", MEDIUMTEXT(), nullable=True),
        sa.Column("custom_css_source", MEDIUMTEXT(), nullable=True),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("suspended_at", sa.DateTime(), nullable=True),
        sa.Column(
            "suspended_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.user_id"),
            nullable=True,
        ),
        sa.Column("suspend_reason", sa.String(255), nullable=True),
        sa.Column("tos_version", sa.String(16), nullable=True),
        sa.Column("tos_accepted_at", sa.DateTime(), nullable=True),
        sa.Column("tos_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("group_id", name="uq_group_sites_group"),
        sa.UniqueConstraint("subdomain", name="uq_group_sites_subdomain"),
    )
    op.create_table(
        "group_site_pages",
        sa.Column("page_id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("group_sites.site_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(40), nullable=False),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("draft_blocks", LONGTEXT(), nullable=False),
        sa.Column("published_blocks", LONGTEXT(), nullable=True),
        sa.Column(
            "schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("site_id", "slug", name="uq_site_page_slug"),
    )


def downgrade() -> None:
    op.drop_table("group_site_pages")
    op.drop_table("group_sites")
