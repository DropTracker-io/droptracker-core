"""Event buy-ins & prize pot (web52a).

- ``web_event_buyins`` — the prize-pot ledger: one row per participant buy-in
  or donation. Only ``status='paid'`` rows count toward the pot
  (``SUM(amount)``). ``amount`` is BigInteger — a large pot exceeds signed-INT
  2.147B (the same lesson as ``web_event_completions.quantity``). A deliberate
  sibling of the completion ledger: pot GP never moves ``web_event_teams.score``.
- ``web_events.buyins_enabled`` — master toggle (cheap scalar every "show the
  pot?" check reads; gates the confirm-on-disable guard).
- ``web_events.prize_config`` — JSON knobs (default buy-in, distribution,
  advertise/show_contributors/allow_leader_mark), merged through
  ``web_api.event_prizes.effective_prize_config()``.

Revision ID: web52a_event_buyins
Revises: web51a_event_visibility
"""
from alembic import op
import sqlalchemy as sa

revision = "web52a_event_buyins"
down_revision = "web51a_event_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_buyins",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("team_id", sa.Integer(), sa.ForeignKey("web_event_teams.id"), nullable=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=True),
        sa.Column("rsn", sa.String(24), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="buyin"),
        # BigInteger: a large pot exceeds signed-INT 2.147B (P1-7).
        sa.Column("amount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pledged"),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("acted_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_web_evt_buyin_event", "web_event_buyins", ["event_id", "status"],
    )
    op.create_index(
        "idx_web_evt_buyin_team", "web_event_buyins", ["event_id", "team_id"],
    )

    op.add_column(
        "web_events",
        sa.Column(
            "buyins_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "web_events",
        sa.Column("prize_config", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_events", "prize_config")
    op.drop_column("web_events", "buyins_enabled")
    op.drop_index("idx_web_evt_buyin_team", table_name="web_event_buyins")
    op.drop_index("idx_web_evt_buyin_event", table_name="web_event_buyins")
    op.drop_table("web_event_buyins")
