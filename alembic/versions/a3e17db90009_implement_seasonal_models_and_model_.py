"""Implement seasonal models and model changes

Revision ID: a3e17db90009
Revises: <PREVIOUS_REVISION_ID>
Create Date: 2026-04-23

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'a3e17db90009'
down_revision = None # <-- REPLACE THIS ID

def upgrade():
    # --- seasonal_collection ---
    op.create_table('seasonal_collection',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), nullable=True),
        sa.Column('date_added', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_seasonal_collection_date_added', 'seasonal_collection', ['date_added'])
    op.create_index('ix_seasonal_collection_item_id', 'seasonal_collection', ['item_id'])
    op.create_index('ix_seasonal_collection_player_id', 'seasonal_collection', ['player_id'])

    # --- seasonal_combat_achievement ---
    op.create_table('seasonal_combat_achievement',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=True),
        sa.Column('ca_id', sa.Integer(), nullable=True),
        sa.Column('date_added', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_seasonal_combat_achievement_date_added', 'seasonal_combat_achievement', ['date_added'])

    # --- seasonal_drops ---
    op.create_table('seasonal_drops',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), nullable=True),
        sa.Column('npc_id', sa.Integer(), nullable=True),
        sa.Column('partition', sa.String(length=50), nullable=True),
        sa.Column('date_added', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_seasonal_drops_date_added', 'seasonal_drops', ['date_added'])
    op.create_index('ix_seasonal_drops_item_id', 'seasonal_drops', ['item_id'])
    op.create_index('ix_seasonal_drops_npc_id', 'seasonal_drops', ['npc_id'])
    op.create_index('ix_seasonal_drops_partition', 'seasonal_drops', ['partition'])
    op.create_index('ix_seasonal_drops_player_id', 'seasonal_drops', ['player_id'])

    # --- seasonal_personal_best ---
    op.create_table('seasonal_personal_best',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=True),
        sa.Column('activity_id', sa.Integer(), nullable=True),
        sa.Column('time_score', sa.Integer(), nullable=True),
        sa.Column('date_added', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    # --- seasonal_player_pets ---
    op.create_table('seasonal_player_pets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=True),
        sa.Column('pet_id', sa.Integer(), nullable=True),
        sa.Column('date_added', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )

    # --- seasonal_quest_completions ---
    op.create_table('seasonal_quest_completions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('player_id', sa.Integer(), nullable=True),
        sa.Column('unique_id', sa.String(length=100), nullable=True),
        sa.Column('date_added', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_seasonal_quest_completions_date_added', 'seasonal_quest_completions', ['date_added'])
    op.create_index('ix_seasonal_quest_completions_player_id', 'seasonal_quest_completions', ['player_id'])
    op.create_index('ix_seasonal_quest_completions_unique_id', 'seasonal_quest_completions', ['unique_id'])

    # --- Schema Fixes ---
    op.create_unique_constraint('uix_notified_single_assoc', 'notified', ['drop_id', 'clog_id', 'ca_id', 'pb_id'])
    op.alter_column('guilds', 'initialized', existing_type=mysql.TINYINT(display_width=1), type_=sa.Integer())

def downgrade():
    op.drop_constraint('uix_notified_single_assoc', 'notified', type_='unique')
    op.drop_table('seasonal_quest_completions')
    op.drop_table('seasonal_player_pets')
    op.drop_table('seasonal_personal_best')
    op.drop_table('seasonal_drops')
    op.drop_table('seasonal_combat_achievement')
    op.drop_table('seasonal_collection')
