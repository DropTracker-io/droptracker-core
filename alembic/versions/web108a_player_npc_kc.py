"""player_npc_kc — durable per-player-per-NPC kill-count watermark

The KC milestone feature ("1st kill, then every Nth") needs an authoritative
"highest KC we have ever seen" per (player, npc). drops.kill_count cannot serve:
it is sparse (~63% of notified drops, post-web76a only) and a milestone check
must be a CROSSING test against a stored previous value, never a membership
test against whatever a single submission happens to carry.

A DB table rather than Redis because the failure mode of losing this state is
not cache-shaped: a lost watermark re-arms "1st kill" announcements and
swallows every milestone crossed inside the loss window. The row is advanced in
the same transaction as the notification enqueue, so a crash can never fire a
milestone twice or advance the watermark without enqueueing.

Main-world only, plugin submissions only (see data/submissions/kc_milestones).

Revision ID: web108a_player_npc_kc
Revises: web107a_effort_clue_rolls
"""
from alembic import op
import sqlalchemy as sa

revision = "web108a_player_npc_kc"
down_revision = "web107a_effort_clue_rolls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "player_npc_kc",
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("npc_id", sa.Integer(), nullable=False),
        sa.Column("kill_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("player_id", "npc_id"),
    )
    # Future per-boss leaderboards read by npc; the PK already covers the
    # (player, npc) hot-path lookup.
    op.create_index("ix_player_npc_kc_npc", "player_npc_kc", ["npc_id"])


def downgrade():
    op.drop_index("ix_player_npc_kc_npc", table_name="player_npc_kc")
    op.drop_table("player_npc_kc")
