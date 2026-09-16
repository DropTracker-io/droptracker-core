"""add group point timed events table

Revision ID: 6c0b93fe767a
Revises: 1a6e66e2e318
Create Date: 2026-02-10 14:07:24.985199

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6c0b93fe767a'
down_revision: Union[str, None] = '1a6e66e2e318'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'group_point_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('start_time_unix', sa.Integer(), nullable=False),
        sa.Column('end_time_unix', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=125), nullable=False),
        sa.Column('target_type', sa.String(length=125), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=True),
        sa.Column('operation', sa.String(length=125), nullable=False),
        sa.Column('operation_value', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('date_added', sa.DateTime(), nullable=True),
        sa.Column('date_updated', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('group_point_events')
