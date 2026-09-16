"""Subscription tier flair: cosmetic display style for subscribed groups.

Adds ``subscription_tiers.flair`` — the visual style a tier grants to a group
wherever its name appears on the website (leaderboards, profile, search).
Values: none|bronze|gold|amethyst|dragon (see web_api/tier_flair.py). Existing
rows default to 'none' (renders like a free group).
"""

from alembic import op
import sqlalchemy as sa


revision = "web27a_tier_flair"
down_revision = "web26a_submission_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_tiers",
        sa.Column(
            "flair",
            sa.String(length=16),
            nullable=False,
            server_default="none",
        ),
    )


def downgrade() -> None:
    op.drop_column("subscription_tiers", "flair")
