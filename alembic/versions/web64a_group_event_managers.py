"""Group-scoped 'event manager' role grant table.

Backs the Event Manager role (db/models/web.GroupEventManager): a member the
group's admins trust to fully manage the group's EVENTS without any group-admin
access. Deliberately a separate table from group_admins so it never flows
through resolve_group_role / assert_group_admin.

Revision ID: web64a_group_event_managers
Revises: web63a_boardgame_economy_rebalance
"""
from alembic import op
import sqlalchemy as sa

revision = "web64a_group_event_managers"
down_revision = "web63a_boardgame_economy_rebalance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_event_managers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["granted_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uix_group_event_manager"),
    )
    op.create_index("idx_group_event_manager_group", "group_event_managers",
                    ["group_id"])
    op.create_index("idx_group_event_manager_user", "group_event_managers",
                    ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_group_event_manager_user", table_name="group_event_managers")
    op.drop_index("idx_group_event_manager_group", table_name="group_event_managers")
    op.drop_table("group_event_managers")
