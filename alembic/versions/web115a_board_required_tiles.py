"""Board game: the "special" tile kind becomes "required".

"special" never had semantics — six drafts marked tiles with it and nothing
read it. "required" (2026-09) is a checkpoint: a team whose move would carry it
past the tile stops on it instead and must complete its task before rolling
on. The code reads legacy rows through LEGACY_BOARD_TILE_KIND_ALIASES, so this
rewrite can run before or after the deploy; it just makes the stored value
match the vocabulary.

Data-only — no schema change.

Revision ID: web115a_board_required_tiles
Revises: web114a_event_point_awards
"""
from alembic import op

revision = "web115a_board_required_tiles"
down_revision = "web114a_event_point_awards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE web_event_board_tiles SET tile_kind = 'required' "
        "WHERE tile_kind = 'special'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE web_event_board_tiles SET tile_kind = 'special' "
        "WHERE tile_kind = 'required'"
    )
