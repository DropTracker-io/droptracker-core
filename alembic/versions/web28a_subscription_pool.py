"""Subscription pool: multi-payer group legs + payments ledger.

Group subscriptions become contribution "legs": a group may hold many rows,
each an independent recurring payment owned by a payer. The group's effective
tier is computed from the sum of its live legs (db/entitlements.py). Adds the
``subscription_payments`` ledger (webhook/IPN-fed) powering the superadmin
monetization dashboard.

Backfills ``amount_cents`` on existing legs from their tier's price so pool
math is correct for legacy PayPal/manual rows.
"""

from alembic import op
import sqlalchemy as sa


revision = "web28a_subscription_pool"
down_revision = "web27a_tier_flair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per group -> many legs per group. MySQL refuses to drop an
    # index a foreign key depends on, so the replacement non-unique index
    # (which also keeps effective-tier lookups cheap) must exist BEFORE the
    # unique constraint goes away.
    op.create_index("ix_group_subscriptions_group_id", "group_subscriptions", ["group_id"])
    op.drop_constraint("uix_group_subscription", "group_subscriptions", type_="unique")
    op.add_column(
        "group_subscriptions",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
    )
    op.add_column(
        "group_subscriptions",
        sa.Column("amount_cents", sa.Integer(), nullable=True),
    )

    # Legacy rows contribute their tier's full price to the pool.
    op.get_bind().execute(
        sa.text(
            "UPDATE group_subscriptions gs "
            "JOIN subscription_tiers t ON t.`key` = gs.tier_key "
            "SET gs.amount_cents = t.price_cents "
            "WHERE gs.amount_cents IS NULL"
        )
    )

    op.create_table(
        "subscription_payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("tier_key", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False, server_default="USD"),
        sa.Column("external_id", sa.String(length=191), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False, server_default="payment"),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("external_id", name="uix_subscription_payment_external"),
    )
    op.create_index("ix_subscription_payments_paid_at", "subscription_payments", ["paid_at"])


def downgrade() -> None:
    op.drop_index("ix_subscription_payments_paid_at", table_name="subscription_payments")
    op.drop_table("subscription_payments")
    op.drop_column("group_subscriptions", "amount_cents")
    op.drop_column("group_subscriptions", "user_id")
    # NOTE: restoring the unique constraint would fail if multiple legs exist.
    # Created before the non-unique index is dropped (the FK needs an index).
    op.create_unique_constraint("uix_group_subscription", "group_subscriptions", ["group_id"])
    op.drop_index("ix_group_subscriptions_group_id", table_name="group_subscriptions")
