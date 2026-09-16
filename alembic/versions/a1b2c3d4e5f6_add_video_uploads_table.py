"""add video_uploads table and drops.video_url column

Revision ID: a1b2c3d4e5f6
Revises: 6d599081fc8e
Create Date: 2026-02-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '6d599081fc8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create video_uploads table
    op.create_table(
        'video_uploads',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=False),
        sa.Column('video_key', sa.String(500), nullable=False),
        sa.Column('final_key', sa.String(500), nullable=True),
        sa.Column('video_url', sa.String(500), nullable=True),
        sa.Column('fps', sa.Integer(), nullable=False, server_default='20'),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('error_message', sa.String(1000), nullable=True),
        sa.Column('file_size_raw', sa.BigInteger(), nullable=True),
        sa.Column('file_size_final', sa.BigInteger(), nullable=True),
        sa.Column('drop_id', sa.Integer(), nullable=True),
        sa.Column('submission_type', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=True, server_default=sa.text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['player_id'], ['players.player_id'], ),
        sa.ForeignKeyConstraint(['drop_id'], ['drops.drop_id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # Create indexes
    op.create_index('idx_video_status', 'video_uploads', ['status'])
    op.create_index('idx_video_player_date', 'video_uploads', ['player_id', 'created_at'])
    op.create_index('idx_video_key', 'video_uploads', ['video_key'], unique=True)
    op.create_index(op.f('ix_video_uploads_player_id'), 'video_uploads', ['player_id'])
    op.create_index(op.f('ix_video_uploads_drop_id'), 'video_uploads', ['drop_id'])

    # Add video_url column to drops table
    op.add_column('drops', sa.Column('video_url', sa.String(500), nullable=True))


def downgrade() -> None:
    # Remove video_url column from drops table
    op.drop_column('drops', 'video_url')

    # Drop indexes and table
    op.drop_index(op.f('ix_video_uploads_drop_id'), table_name='video_uploads')
    op.drop_index(op.f('ix_video_uploads_player_id'), table_name='video_uploads')
    op.drop_index('idx_video_key', table_name='video_uploads')
    op.drop_index('idx_video_player_date', table_name='video_uploads')
    op.drop_index('idx_video_status', table_name='video_uploads')
    op.drop_table('video_uploads')
