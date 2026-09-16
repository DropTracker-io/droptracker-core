"""Unique index on users.discord_id (web56a).

The users table had no index on discord_id at all, so every login/creation
lookup was a full table scan — and nothing stopped concurrent create paths
(web OAuth, bot try_create_user, group-creation _ensure_user) from inserting
duplicate rows for one Discord account. 17 such duplicates were merged on
2026-07-19 (backup: /home/debian/db_backups/users_dupe_merge_20260719.json)
before this constraint was added. MySQL permits multiple NULLs in a unique
index, so the one discord_id IS NULL row is unaffected.

Revision ID: web56a_users_discord_id_unique
Revises: web55a_plugin_notification_prefs
"""
from alembic import op

revision = "web56a_users_discord_id_unique"
down_revision = "web55a_plugin_notification_prefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_users_discord_id", "users", ["discord_id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_discord_id", "users", type_="unique")
