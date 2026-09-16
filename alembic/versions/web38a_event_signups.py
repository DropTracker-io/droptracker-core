"""Event sign-up pool (self-service opt-in).

Adds ``web_event_signups`` — a player's self-service opt-in to an event. In
``signup_pool`` formation mode the sign-up carries no team until an admin sorts
the pool; in self_join/auto_assign placement is immediate but the row still
records the opt-in. Additive: events that never use self sign-up (admin_assign,
the historical default) write no rows here, so their behavior is unchanged.

The new ``signup_pool`` value of ``web_events.formation_mode`` needs no schema
change (the column already stores free-form strings).
"""

from alembic import op
import sqlalchemy as sa


revision = "web38a_event_signups"
down_revision = "web37a_manual_submission_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_signups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
        # web|discord
        sa.Column("source", sa.String(length=16), nullable=False, server_default="web"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_web_event_signup", "web_event_signups", ["event_id", "player_id"], unique=True
    )
    op.create_index("idx_web_event_signup_event", "web_event_signups", ["event_id"])


def downgrade() -> None:
    op.drop_index("idx_web_event_signup_event", table_name="web_event_signups")
    op.drop_index("uq_web_event_signup", table_name="web_event_signups")
    op.drop_table("web_event_signups")
