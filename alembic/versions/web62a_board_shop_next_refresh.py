"""Board-game shop: scheduled time-based restock column.

Adds ``web_event_board_config.shop_next_refresh_at`` — the wall-clock time of
the next time-based shop restock (refresh_mode 'hours'/'days'). Storing the
scheduled moment lets the ``refresh_random`` jitter pick a value once and stay
stable between reads, and unblocks the "restock every X days (at a specific or
random time)" configuration. Turns-mode restock still uses shop_refreshed_turn.

Revision ID: web62a_board_shop_next_refresh
Revises: web61a_boardgame_activate_items
"""
from alembic import op
import sqlalchemy as sa

revision = "web62a_board_shop_next_refresh"
down_revision = "web61a_boardgame_activate_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_event_board_config",
        sa.Column("shop_next_refresh_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_event_board_config", "shop_next_refresh_at")
