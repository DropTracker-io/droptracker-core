"""Add player_deaths and diary_completions tables.

NOTE: not yet applied to production — run `alembic upgrade head` at deploy time.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "web19a_deaths_diaries"
down_revision = "web18a_events_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_deaths",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("region_id", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("world_type", sa.String(length=20), nullable=False, server_default="main"),
        sa.Column("image_url", sa.String(length=300), nullable=True),
        sa.Column("video_url", sa.String(length=500), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), nullable=True, server_default=sa.text("0")),
        sa.Column("unique_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unique_id", name="uq_player_deaths_unique_id"),
    )
    op.create_index("ix_player_deaths_player_id", "player_deaths", ["player_id"], unique=False)
    op.create_index("ix_player_deaths_date_added", "player_deaths", ["date_added"], unique=False)

    op.create_table(
        "diary_completions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("diary_name", sa.String(length=255), nullable=False),
        sa.Column("diary_tier", sa.String(length=20), nullable=True),
        sa.Column("world_type", sa.String(length=20), nullable=False, server_default="main"),
        sa.Column("timestamp", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(length=300), nullable=True),
        sa.Column("video_url", sa.String(length=500), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), nullable=True, server_default=sa.text("0")),
        sa.Column("unique_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unique_id", name="uq_diary_completions_unique_id"),
    )
    op.create_index("ix_diary_completions_player_id", "diary_completions", ["player_id"], unique=False)
    op.create_index("ix_diary_completions_date_added", "diary_completions", ["date_added"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_diary_completions_date_added", table_name="diary_completions")
    op.drop_index("ix_diary_completions_player_id", table_name="diary_completions")
    op.drop_table("diary_completions")
    op.drop_index("ix_player_deaths_date_added", table_name="player_deaths")
    op.drop_index("ix_player_deaths_player_id", table_name="player_deaths")
    op.drop_table("player_deaths")
