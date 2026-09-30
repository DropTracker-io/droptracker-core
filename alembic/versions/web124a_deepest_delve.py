"""player_deepest_delve + the Doom 8+ NPC row (web124a).

``player_deepest_delve``  deepest Doom of Mokhaiotl delve level each player
                          has completed (utils/doom_delve.py)
``npc_list`` 14716        "Doom of Mokhaiotl (Level: 8+)", the game's shared
                          personal best for every level past 8. Those PBs used
                          to fall onto the boss row (14707).

A new table and one row insert, so it is safe to apply live. Guarded, so a
re-run is harmless.

Revision ID: web124a_deepest_delve
Revises: web123a_conquest_fairness
"""
from alembic import op
import sqlalchemy as sa

revision = "web124a_deepest_delve"
down_revision = "web123a_conquest_fairness"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("player_deepest_delve"):
        op.create_table(
            "player_deepest_delve",
            sa.Column("player_id", sa.Integer(), nullable=False),
            sa.Column("deepest_level", sa.Integer(), nullable=False),
            sa.Column("exact", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("achieved_at", sa.DateTime(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("player_id"),
        )
        op.create_index("ix_player_deepest_delve_level", "player_deepest_delve",
                        ["deepest_level"])
    bind.execute(sa.text(
        "INSERT IGNORE INTO npc_list (npc_id, npc_name) "
        "VALUES (14716, 'Doom of Mokhaiotl (Level: 8+)')"
    ))


def downgrade():
    op.drop_index("ix_player_deepest_delve_level", table_name="player_deepest_delve")
    op.drop_table("player_deepest_delve")
