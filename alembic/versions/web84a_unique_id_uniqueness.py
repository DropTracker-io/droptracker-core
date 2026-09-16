"""Enforce unique_id uniqueness on the small dedup tables (web84a).

web80a gave these tables a non-unique index on ``unique_id`` so the dedup
lookup in ``data/submissions/common.ensure_can_create`` would stop table-
scanning. Nothing enforced uniqueness, though: if the code check missed, the
duplicate row was written. It did miss — the check only looked back one hour,
so the 2026-08-02 outage recovery replayed submissions hours later and they
inserted a second time. The code check is now unbounded, and this migration
adds the backstop underneath it, so a replay that slips past the application
(a race between two consumer workers, a future regression) fails its INSERT
instead of silently inflating totals.

Scope: the four SMALL tables only. ``drops`` is deliberately excluded — it is
175M rows / 38.2 GB, where this ALTER is a multi-hour rebuild with real MDL
pileup risk, for a guarantee the (0.3 ms, indexed) code check already provides.
Do not add it without re-measuring.

Feasibility was verified against live data before writing this (2026-08-02).
MySQL/MariaDB permits many NULLs in a UNIQUE index but NOT many empty strings,
so the blocking question was empty values, and there are none:

    table               rows      NULL     empty   dup groups
    personal_best      69,076    22,011        0       3
    collection        267,724    94,973        0       2
    combat_achievement 197,194   80,527        0       5
    player_pets         4,425     1,727        0       1

The duplicate groups must be cleared first or the ALTER fails:
    venv/bin/python -m scripts.dedupe_submission_rows --apply
This migration pre-checks and aborts with that instruction rather than dying
half-applied.

The old non-unique index is dropped in the SAME ALTER that adds the unique one,
so there is never a window without an index and no redundant duplicate index is
left behind.

Revision ID: web84a_unique_id_uniqueness
Revises: web83a_recap_wom_gains
"""
from alembic import op
import sqlalchemy as sa

revision = "web84a_unique_id_uniqueness"
down_revision = "web83a_recap_wom_gains"
branch_labels = None
depends_on = None

# (table, old non-unique index from web80a, new unique index)
TABLES = [
    ("personal_best", "ix_personal_best_unique_id", "uq_personal_best_unique_id"),
    ("collection", "ix_collection_unique_id", "uq_collection_unique_id"),
    ("combat_achievement", "ix_combat_achievement_unique_id", "uq_combat_achievement_unique_id"),
    ("player_pets", "ix_player_pets_unique_id", "uq_player_pets_unique_id"),
]


def _index_exists(table: str, name: str) -> bool:
    row = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM information_schema.STATISTICS "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i "
            "LIMIT 1"
        ),
        {"t": table, "i": name},
    ).first()
    return row is not None


def _duplicate_count(table: str) -> int:
    return op.get_bind().execute(
        sa.text(
            f"SELECT COUNT(*) FROM (SELECT unique_id FROM `{table}` "
            "WHERE unique_id IS NOT NULL AND unique_id <> '' "
            "GROUP BY unique_id HAVING COUNT(*) > 1) d"
        )
    ).scalar()


def _alter(table: str, statement: str) -> None:
    """Run an index change, preferring a fully online rebuild.

    LOCK=NONE keeps intake writing through the change. If the server refuses
    that combination it raises rather than silently taking a stronger lock, so
    fall back explicitly — these tables top out at ~268k rows, where even a
    blocking rebuild is sub-second.
    """
    bind = op.get_bind()
    try:
        bind.execute(sa.text(f"{statement}, ALGORITHM=INPLACE, LOCK=NONE"))
    except Exception:
        bind.execute(sa.text(statement))


def _bound_lock_wait() -> None:
    """Fail fast rather than pile up behind an idle transaction.

    DDL on this box has caused metadata-lock pileups before: an idle web-API
    transaction holds a shared MDL, the ALTER queues behind it, and every
    later query on that table queues behind the ALTER — a table-wide outage
    from a change that should take milliseconds. A short lock_wait_timeout
    turns that into a clean, retryable error instead.
    """
    op.get_bind().execute(sa.text("SET SESSION lock_wait_timeout = 10"))


def upgrade() -> None:
    _bound_lock_wait()
    blocked = {t: n for t, _, _ in TABLES if (n := _duplicate_count(t))}
    if blocked:
        detail = ", ".join(f"{t}: {n} duplicate GUID group(s)" for t, n in blocked.items())
        raise RuntimeError(
            f"Cannot add UNIQUE index while duplicates remain ({detail}). "
            "Clear them first: venv/bin/python -m scripts.dedupe_submission_rows --apply"
        )

    for table, old_index, new_index in TABLES:
        if _index_exists(table, new_index):
            continue
        parts = []
        if _index_exists(table, old_index):
            parts.append(f"DROP INDEX `{old_index}`")
        parts.append(f"ADD UNIQUE INDEX `{new_index}` (unique_id)")
        _alter(table, f"ALTER TABLE `{table}` " + ", ".join(parts))


def downgrade() -> None:
    _bound_lock_wait()
    for table, old_index, new_index in TABLES:
        if not _index_exists(table, new_index):
            continue
        parts = [f"DROP INDEX `{new_index}`"]
        if not _index_exists(table, old_index):
            parts.append(f"ADD INDEX `{old_index}` (unique_id)")
        _alter(table, f"ALTER TABLE `{table}` " + ", ".join(parts))
