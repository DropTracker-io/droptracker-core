"""Widen plugin_manifest_sections.payload to LONGTEXT (web100a).

web99a created the column as TEXT, faithfully reproducing the model. The model
was wrong: the ``combat_achievement_tasks`` section is ~144KB (every combat
achievement task with its name, tier, monster and varp/bit pair), and MySQL's
TEXT ceiling is 65,535 bytes. Seeding it failed with "Data too long for column
'payload' at row 2", which meant the plugin would have fetched an empty manifest
and silently synced nothing.

LONGTEXT matches how the rest of the schema stores JSON payload blobs —
``recaps.payload`` and ``group_component_layouts.layout`` are both LONGTEXT for
the same reason.

Revision ID: web100a_manifest_payload_longtext
Revises: web99a_player_state
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "web100a_manifest_payload_longtext"
down_revision = "web99a_player_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "plugin_manifest_sections",
        "payload",
        existing_type=sa.Text(),
        type_=mysql.LONGTEXT(),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Lossy by nature: any section over 64KB cannot fit in TEXT.
    op.alter_column(
        "plugin_manifest_sections",
        "payload",
        existing_type=mysql.LONGTEXT(),
        type_=sa.Text(),
        existing_nullable=False,
    )
