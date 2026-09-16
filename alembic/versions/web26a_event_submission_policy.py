"""Events v2: per-event submission source policy.

Adds web_events.submission_policy ("all" | "confirm_non_api" | "api_only")
controlling whether non-API intake (webhook-bot fallback) can drive automatic
task progress, must queue for admin confirmation, or is ignored entirely.
"""

from alembic import op
import sqlalchemy as sa


revision = "web26a_submission_policy"
down_revision = "web25a_matched_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("submission_policy", sa.String(16), nullable=False,
                  server_default="all"),
    )


def downgrade() -> None:
    op.drop_column("web_events", "submission_policy")
