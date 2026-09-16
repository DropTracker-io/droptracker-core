"""add submission_unique_id and video_url columns

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-02-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Allow generic linkage/backfill of videos to any submission type
    op.add_column("video_uploads", sa.Column("submission_unique_id", sa.String(255), nullable=True))
    op.create_index("idx_video_submission_unique_id", "video_uploads", ["submission_unique_id"])

    # Persist video URLs on additional submission tables
    op.add_column("personal_best", sa.Column("video_url", sa.String(500), nullable=True))
    op.add_column("combat_achievement", sa.Column("video_url", sa.String(500), nullable=True))
    op.add_column("collection", sa.Column("video_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("collection", "video_url")
    op.drop_column("combat_achievement", "video_url")
    op.drop_column("personal_best", "video_url")

    op.drop_index("idx_video_submission_unique_id", table_name="video_uploads")
    op.drop_column("video_uploads", "submission_unique_id")

