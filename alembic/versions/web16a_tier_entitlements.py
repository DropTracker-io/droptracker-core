"""Tier entitlements column on subscription_tiers (Task 15)."""

from alembic import op
import sqlalchemy as sa


revision = "web16a_entitlements"
down_revision = "web15a_docs_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_tiers",
        sa.Column("entitlements", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscription_tiers", "entitlements")
