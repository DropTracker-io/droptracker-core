"""Events v2: matched item name on completion ledger rows.

Drives distinct-item progress for all_of/assembly item tasks — previously any
listed item's drop QUANTITY folded into progress against a threshold of
len(items), so one 1,338-coins drop completed a 3-item "collect all" task.
"""

from alembic import op
import sqlalchemy as sa


revision = "web25a_matched_target"
down_revision = "web24a_point_seasons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_completions",
        sa.Column("matched_target", sa.String(120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_event_completions", "matched_target")
