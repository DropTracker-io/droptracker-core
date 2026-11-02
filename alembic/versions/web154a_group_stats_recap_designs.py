"""Clan stats + customizable recaps (web154a, suggestion #224).

- ``recap_wom_gains.metrics`` — every Wise Old Man metric a member gained in
  the month (skills XP, boss KC, activities, EHP, EHB) as compact JSON,
  ``{"metric": [gained, start, end]}`` for non-zero gains only. The bulk-gained
  call that fills ``ehb_gained`` already returns all 114 metrics; until now the
  rest were thrown away. NULL marks a row harvested before this column existed,
  which the harvest treats as "not harvested yet" so the month is fetched again
  once, on demand.
- ``recap_designs`` — a clan's recap layout. ``period = ''`` is the clan's
  default for every month, a ``'YYYY-MM'`` row overrides it for that month
  alone. ``approved_at`` / ``approved_by`` on a month row record an admin's
  approval when the clan holds recaps for review.

Revision ID: web154a_group_stats_recap_designs
Revises: web153a_discord_relay
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "web154a_group_stats_recap_designs"
down_revision = "web153a_discord_relay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recap_wom_gains",
        sa.Column("metrics", mysql.MEDIUMTEXT(), nullable=True),
    )
    op.create_table(
        "recap_designs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(7), nullable=False, server_default=""),
        sa.Column("design", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.UniqueConstraint("group_id", "period", name="uq_recap_design_group_period"),
    )


def downgrade() -> None:
    op.drop_table("recap_designs")
    op.drop_column("recap_wom_gains", "metrics")
