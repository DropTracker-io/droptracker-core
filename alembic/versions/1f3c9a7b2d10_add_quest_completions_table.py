"""Add quest_completions table."""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "1f3c9a7b2d10"
down_revision = "9f0c1d2e3a4b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quest_completions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("quest_name", sa.String(length=255), nullable=False),
        sa.Column("quests_completed", sa.Integer(), nullable=True),
        sa.Column("total_quests", sa.Integer(), nullable=True),
        sa.Column("completion_percentage", sa.String(length=20), nullable=True),
        sa.Column("quest_points", sa.Integer(), nullable=True),
        sa.Column("total_quest_points", sa.Integer(), nullable=True),
        sa.Column("qp_percentage", sa.String(length=20), nullable=True),
        sa.Column("timestamp", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(length=300), nullable=True),
        sa.Column("video_url", sa.String(length=500), nullable=True),
        sa.Column("date_added", sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column("used_api", sa.Boolean(), nullable=True, server_default=sa.text("0")),
        sa.Column("unique_id", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["player_id"], ["players.player_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unique_id", name="uq_quest_completions_unique_id"),
    )
    op.create_index("ix_quest_completions_player_id", "quest_completions", ["player_id"], unique=False)
    op.create_index("ix_quest_completions_date_added", "quest_completions", ["date_added"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_quest_completions_date_added", table_name="quest_completions")
    op.drop_index("ix_quest_completions_player_id", table_name="quest_completions")
    op.drop_table("quest_completions")

