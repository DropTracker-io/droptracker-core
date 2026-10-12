"""Per-activity death switches: Last Man Standing and the PK tutor (web134a).

``notify_deaths_lms`` and ``notify_deaths_pk_tutor`` are group_configurations
keys (registry-validated), so there is no schema change. Both are decided by
region before the safe-death rule (db/death_filter.py) and default to off.

LMS deaths used to come in under ``notify_deaths_safe``. Every group that has
that switch on gets ``notify_deaths_lms`` on too, so its feed does not change
until a leader decides otherwise. The PK tutor is new content that posted
dangerous deaths to everyone, so it is not carried over: it starts off.

Data only. Inserts a row only where the group has none, so a re-run is
harmless and a leader's later choice is never overwritten.

Revision ID: web134a_death_activity_switches
Revises: web133a_notice_discord_target
"""
from alembic import op
import sqlalchemy as sa

revision = "web134a_death_activity_switches"
down_revision = "web133a_notice_discord_target"
branch_labels = None
depends_on = None

#: (existing switch, new switch) for the main and seasonal key sets.
_CARRY_OVER = (
    ("notify_deaths_safe", "notify_deaths_lms"),
    ("seasonal_notify_deaths_safe", "seasonal_notify_deaths_lms"),
)


def upgrade() -> None:
    conn = op.get_bind()
    for old_key, new_key in _CARRY_OVER:
        # Truthy exactly as utils.group_config.is_truthy reads it.
        conn.execute(
            sa.text(
                """
                INSERT INTO group_configurations (group_id, config_key, config_value, updated_at)
                SELECT gc.group_id, :new_key, '1', NOW()
                FROM group_configurations gc
                WHERE gc.config_key = :old_key
                  AND LOWER(TRIM(gc.config_value)) IN ('true', '1')
                  AND NOT EXISTS (
                      SELECT 1 FROM group_configurations existing
                      WHERE existing.group_id = gc.group_id
                        AND existing.config_key = :new_key
                  )
                """
            ),
            {"old_key": old_key, "new_key": new_key},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            DELETE FROM group_configurations
            WHERE config_key IN (
                'notify_deaths_lms', 'seasonal_notify_deaths_lms',
                'notify_deaths_pk_tutor', 'seasonal_notify_deaths_pk_tutor'
            )
            """
        )
    )
