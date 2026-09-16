"""Group subscriptions / tiers (backend Task 11).

Recurring-subscription model that replaces the points feature store.
"""

from alembic import op
import sqlalchemy as sa


revision = "web11a_subs"
down_revision = "web09a_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscription_tiers",
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("interval", sa.String(length=8), nullable=False, server_default="month"),
        sa.Column("features", sa.Text(), nullable=True),
        sa.Column("recommended", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("provider_price_id", sa.String(length=120), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "group_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("tier_key", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("provider", sa.String(length=16), nullable=True),
        sa.Column("provider_customer_id", sa.String(length=120), nullable=True),
        sa.Column("provider_subscription_id", sa.String(length=120), nullable=True),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["tier_key"], ["subscription_tiers.key"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", name="uix_group_subscription"),
    )


def downgrade() -> None:
    op.drop_table("group_subscriptions")
    op.drop_table("subscription_tiers")
