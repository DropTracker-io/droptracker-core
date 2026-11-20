"""player_music_varps (web165a).

Music track unlocks from state sync (suggestion #252): the raw musicmulti_N
varps plugin 6.1.31 sends, plus the decoded counts. Decoded against the
music_tracks manifest section (scripts/music_tracks.json, from the game cache).
See db/models/player_state.PlayerMusicVarps and utils/music_tracks.py.

Revision ID: web165a_player_music_varps
Revises: web164a_bank_bonds
"""
from alembic import op
import sqlalchemy as sa

revision = "web165a_player_music_varps"
down_revision = "web164a_bank_bonds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "player_music_varps" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "player_music_varps",
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), primary_key=True),
        sa.Column("varps", sa.Text(), nullable=False),
        sa.Column("tracks_unlocked", sa.Integer(), nullable=True),
        sa.Column("tracks_total", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("player_music_varps")
