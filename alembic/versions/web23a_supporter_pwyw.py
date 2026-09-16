"""Supporter pay-what-you-want: amount_cents + tier repricing/perks.

The supporter tier becomes pay-what-you-want with a $5/month minimum
(price_cents now means "minimum"), and gains the personal
``video_submissions`` entitlement — supporters can upload video clips of
their own submissions independent of their groups' tiers.
"""

import json

from alembic import op
import sqlalchemy as sa


revision = "web23a_supporter_pwyw"
down_revision = "web22a_user_subs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_subscriptions",
        sa.Column("amount_cents", sa.Integer(), nullable=True),
    )

    entitlements = json.dumps(
        {"dm_submissions": True, "supporter_flair": True, "video_submissions": True}
    )
    features = json.dumps(
        [
            "Choose your own amount — anything from $5/month",
            "Video capture for your own submissions, even if your group's plan doesn't include it",
            "Discord DMs for your own submissions, with your own filters",
            "Supporter flair on your public profile",
            "Directly support DropTracker's development and hosting",
        ]
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE subscription_tiers SET price_cents = :price, entitlements = :ents, "
            "features = :features, description = :description WHERE `key` = 'supporter'"
        ),
        {
            "price": 500,
            "ents": entitlements,
            "features": features,
            "description": (
                "Support DropTracker personally at any amount you choose (from $5/month) "
                "and unlock perks for you — independent of any group subscription."
            ),
        },
    )


def downgrade() -> None:
    op.drop_column("user_subscriptions", "amount_cents")
