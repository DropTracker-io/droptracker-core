"""Clan-vs-clan events (Implementation Plan B, Phase 1) — all additive.

- `web_events.mode` ("standard" | "clan_vs_clan", server_default "standard"):
  existing rows read back unchanged as standard events.
- `web_event_teams.group_id` (nullable): the clan a team represents in
  clan-vs-clan mode; NULL on every standard/global event team.
- `web_event_groups`: the participant roster for clan-vs-clan events
  (host/opponent, invited/accepted/declined). Standard/global events write
  NO rows here — `web_events.group_id` remains their sole owner.

No backfill: standard and global events never read any of these.
"""

from alembic import op
import sqlalchemy as sa


revision = "web32a_clan_vs_clan"
down_revision = "web31a_item_value_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_events",
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="standard"),
    )
    op.add_column(
        "web_event_teams",
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=True),
    )
    op.create_table(
        "web_event_groups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("web_events.id"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.group_id"), nullable=False),
        # host|opponent
        sa.Column("role", sa.String(length=16), nullable=False, server_default="opponent"),
        # invited|accepted|declined
        sa.Column("status", sa.String(length=16), nullable=False, server_default="invited"),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("users.user_id"), nullable=True),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_web_event_group_part", "web_event_groups", ["event_id", "group_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_web_event_group_part", table_name="web_event_groups")
    op.drop_table("web_event_groups")
    op.drop_column("web_event_teams", "group_id")
    op.drop_column("web_events", "mode")
