"""User-level premium: user_subscriptions table + tier scope + supporter tier seed."""

import json

from alembic import op
import sqlalchemy as sa


revision = "web22a_user_subs"
down_revision = "web21a_ticket_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_tiers",
        sa.Column("scope", sa.String(8), nullable=False, server_default="group"),
    )

    op.create_table(
        "user_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column(
            "tier_key",
            sa.String(40),
            sa.ForeignKey("subscription_tiers.key"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("provider", sa.String(16), nullable=True),
        sa.Column("provider_customer_id", sa.String(120), nullable=True),
        sa.Column("provider_subscription_id", sa.String(120), nullable=True),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", name="uix_user_subscription"),
    )

    # Seed the supporter tier so checkout works immediately; superadmins can
    # rename/reprice it on /admin/tiers.
    entitlements = json.dumps({"dm_submissions": True, "supporter_flair": True})
    features = json.dumps(
        [
            "Discord DMs for your own submissions, with your own filters",
            "Supporter flair on your public profile",
            "Directly support DropTracker's development and hosting",
        ]
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO subscription_tiers "
            "(`key`, name, description, scope, price_cents, currency, `interval`, "
            " features, entitlements, recommended, active, created_at, updated_at) "
            "VALUES (:key, :name, :description, :scope, :price_cents, :currency, "
            " :interval, :features, :entitlements, 0, 1, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE scope = :scope"
        ),
        {
            # "Personal" prefix: the group-scoped 'basic' tier already
            # displays as "Supporter" on the pricing page.
            "key": "supporter",
            "name": "Personal Supporter",
            "description": "Personal perks for supporting DropTracker — independent of any group subscription.",
            "scope": "user",
            "price_cents": 300,
            "currency": "USD",
            "interval": "month",
            "features": features,
            "entitlements": entitlements,
        },
    )


def downgrade() -> None:
    op.drop_table("user_subscriptions")
    op.drop_column("subscription_tiers", "scope")
