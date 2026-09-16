"""Timed point boosts: multi-item targets.

A boost row historically pointed at a single item/npc via target_id, which
forced admins to clone the same boost once per item (e.g. three "double
points CoX" rows for dust / kit / Olmlet). target_ids holds a JSON array of
ids for multi-target boosts; single-target rows keep using target_id.

Revision ID: web92a_boost_target_ids
Revises: web91a_site_modes
"""
from alembic import op
import sqlalchemy as sa

revision = "web92a_boost_target_ids"
down_revision = "web91a_site_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_point_events", sa.Column("target_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("group_point_events", "target_ids")
