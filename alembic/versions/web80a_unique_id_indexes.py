"""Index unique_id on the dedup-checked submission tables (web80a).

Every submission runs ``_check_existing`` (data/submissions/common.py): a
lookup by ``unique_id`` within the last hour, per drop, before insert. On
``drops`` (~176M rows) there was NO index on ``unique_id``, so each check
range-scanned ``ix_drops_date_added`` — ~73k rows / ~120ms of event-loop-
blocking I/O per drop at 2026-07-31 traffic. That put a hard ~40k drops/hour
ceiling on intake and left ``webhook:queue`` 107 minutes behind at evening
peak (and it degraded with traffic: busier hour → wider 1h window → slower
scan). The video-attach lookup (``_try_attach_video_url_to_drop``) hits the
same missing index.

The newer dedup tables (quest_completions, player_deaths, diary_completions,
seasonal mirrors) were created with the index; these five predate that habit.

Applied online 2026-08-01 (~00:50 UTC) with ALGORITHM=INPLACE, LOCK=NONE —
this migration is written to be a safe no-op where the index already exists.

Revision ID: web80a_unique_id_indexes
Revises: web79a_recap_deliveries
"""
from alembic import op
import sqlalchemy as sa

revision = "web80a_unique_id_indexes"
down_revision = "web79a_recap_deliveries"
branch_labels = None
depends_on = None

INDEXES = [
    ("drops", "ix_drops_unique_id"),
    ("collection", "ix_collection_unique_id"),
    ("combat_achievement", "ix_combat_achievement_unique_id"),
    ("personal_best", "ix_personal_best_unique_id"),
    ("player_pets", "ix_player_pets_unique_id"),
]


def _index_exists(table: str, name: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT 1 FROM information_schema.STATISTICS "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i "
            "LIMIT 1"
        ),
        {"t": table, "i": name},
    ).first()
    return row is not None


def upgrade() -> None:
    for table, name in INDEXES:
        if _index_exists(table, name):
            continue
        # LOCK=NONE keeps intake writing during the (long, for drops) build.
        op.execute(
            f"ALTER TABLE `{table}` ADD INDEX `{name}` (unique_id), "
            f"ALGORITHM=INPLACE, LOCK=NONE"
        )


def downgrade() -> None:
    for table, name in INDEXES:
        if _index_exists(table, name):
            op.execute(f"ALTER TABLE `{table}` DROP INDEX `{name}`")
