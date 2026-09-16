"""Custom points web surface: group_point_seasons table + player_points leaderboard index."""

from alembic import op
import sqlalchemy as sa


revision = "web24a_point_seasons"
down_revision = "web23a_supporter_pwyw"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_point_seasons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("groups.group_id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_group_point_seasons_group",
        "group_point_seasons",
        ["group_id", "start_at"],
    )

    # Covering index for timeframe leaderboards:
    # WHERE group_id = ? AND date_added >= ? [AND date_added < ?]
    # GROUP BY player_id / SUM(amount) resolves from the index alone.
    op.create_index(
        "ix_player_points_group_date",
        "player_points",
        ["group_id", "date_added", "player_id", "amount"],
    )


def downgrade() -> None:
    op.drop_index("ix_player_points_group_date", table_name="player_points")
    op.drop_index("ix_group_point_seasons_group", table_name="group_point_seasons")
    op.drop_table("group_point_seasons")
