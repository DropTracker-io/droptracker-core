"""Migrate dead *_channel_id group-config rows to the canonical
channel_id_to_post_* keys the notification service actually reads.

The web config editor's registry originally used *_channel_id key names that
no consumer read (only lootboard_channel_id and announcements_channel_id are
real). Any channel a group saved through the editor under a dead key is copied
to the canonical key — unless the canonical key already has a value, which
wins because it is the one that has been driving notifications all along.
The dead rows are deleted afterwards.

NOTE: not yet applied to production — run `alembic upgrade head` at deploy time.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "web20a_channel_key_rename"
down_revision = "web19a_deaths_diaries"
branch_labels = None
depends_on = None


KEY_RENAMES = [
    ("drop_channel_id", "channel_id_to_post_loot"),
    ("level_channel_id", "channel_id_to_post_levels"),
    ("pb_channel_id", "channel_id_to_post_pb"),
    ("ca_channel_id", "channel_id_to_post_ca"),
    ("pet_channel_id", "channel_id_to_post_pets"),
    ("quest_channel_id", "channel_id_to_post_quests"),
]


def upgrade() -> None:
    conn = op.get_bind()
    for old_key, new_key in KEY_RENAMES:
        # Copy old value into the canonical key where the canonical row is
        # missing entirely for that group.
        conn.execute(
            sa.text(
                """
                INSERT INTO group_configurations (group_id, config_key, config_value, updated_at)
                SELECT gc.group_id, :new_key, gc.config_value, NOW()
                FROM group_configurations gc
                WHERE gc.config_key = :old_key
                  AND gc.config_value IS NOT NULL AND gc.config_value != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM group_configurations existing
                      WHERE existing.group_id = gc.group_id
                        AND existing.config_key = :new_key
                  )
                """
            ),
            {"old_key": old_key, "new_key": new_key},
        )
        # Fill canonical rows that exist but are empty.
        conn.execute(
            sa.text(
                """
                UPDATE group_configurations new_row
                JOIN group_configurations old_row
                  ON old_row.group_id = new_row.group_id
                 AND old_row.config_key = :old_key
                SET new_row.config_value = old_row.config_value,
                    new_row.updated_at = NOW()
                WHERE new_row.config_key = :new_key
                  AND (new_row.config_value IS NULL OR new_row.config_value = '')
                  AND old_row.config_value IS NOT NULL AND old_row.config_value != ''
                """
            ),
            {"old_key": old_key, "new_key": new_key},
        )
        # Remove the dead rows so nothing writes/reads them again.
        conn.execute(
            sa.text("DELETE FROM group_configurations WHERE config_key = :old_key"),
            {"old_key": old_key},
        )


def downgrade() -> None:
    # Data migration; the dead keys are intentionally not restored.
    pass
