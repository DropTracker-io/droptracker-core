"""Event clan-point awards (web114a).

Events can pay out a clan's custom points (``player_points``) for placement
(1st/2nd/3rd…) and for participation, priced from EHE (Efficient Hours towards
Event). Two tables:

``web_event_point_configs`` — one row per (event, clan): that clan's payout
config (JSON) plus the award state machine (pending / deferred / awarded /
revoked). Per clan rather than a ``web_events`` column because a clan-vs-clan
event spans several point economies, and each participating clan decides what
its own members earn in its own ledger.

``web_event_point_awards`` — the award ledger, one row per (event, clan,
player, kind), naming the ``player_points`` row it wrote. That link is what
makes awarding re-runnable: a re-sync after a post-event revoke edits the row
in place instead of stacking a second award, and a revoke removes exactly what
the event paid. ``player_points_id`` has no FK on purpose — a group's points
reset deletes those rows and must not be blocked (or undone) by this table.

See ``services/event_point_awards.py``.

Revision ID: web114a_event_point_awards
Revises: web113a_slayer_task_completions
"""
from alembic import op
import sqlalchemy as sa

revision = "web114a_event_point_awards"
down_revision = "web113a_slayer_task_completions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_event_point_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=False),
        sa.Column("config", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("awarded_at", sa.DateTime(), nullable=True),
        sa.Column("awarded_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"),
                  nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_web_evt_point_cfg", "web_event_point_configs",
        ["event_id", "group_id"], unique=True,
    )
    # The lifecycle sweep's retry pass reads deferred rows every tick.
    op.create_index(
        "idx_web_evt_point_cfg_status", "web_event_point_configs", ["status"],
    )

    op.create_table(
        "web_event_point_awards",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=False),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"),
                  nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("team_id", sa.Integer(), nullable=True),
        sa.Column("place", sa.Integer(), nullable=True),
        sa.Column("hours", sa.Float(), nullable=True),
        sa.Column("player_points_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    # One award per player per kind per clan per event — the idempotency key
    # every award / re-sync path upserts on.
    op.create_index(
        "uq_web_evt_point_award", "web_event_point_awards",
        ["event_id", "group_id", "player_id", "kind"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_web_evt_point_award", table_name="web_event_point_awards")
    op.drop_table("web_event_point_awards")
    op.drop_index("idx_web_evt_point_cfg_status", table_name="web_event_point_configs")
    op.drop_index("uq_web_evt_point_cfg", table_name="web_event_point_configs")
    op.drop_table("web_event_point_configs")
