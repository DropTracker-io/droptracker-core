"""A player's combat achievement point total: the one writer both sources use.

``player_ca_varps.points`` is what the Data API's ``combat_achievements``
section reports, and the tier is derived from it at read time. Two readings of
the number arrive, and they are not equally good:

* ``game`` — the total the game itself reports (varbit 14815), sent with every
  combat achievement completion (``data/submissions/ca.py``). The truth at
  the moment it was taken, and far more frequent than a sync.
* ``sync`` — what the account sync's completion bits are worth, counted
  against the task registry (``services.state_sync.combat_achievement_points``,
  called from ``api/routes/state_sync.py``). Exact when the registry is
  current, but a registry that lags a game update misses the new tasks, and a
  sync taken at login before the game has sent the varps counts nothing: two
  players with completed tasks on record have synced all-zero bits.

The rule for which reading to keep follows from one fact: a player's points
never go down.

1. A higher reading replaces a lower one, whatever its age. Points only rise,
   so a higher total is proof the lower one is stale; a lower reading can be
   an undercount, never an overcount.
2. A lower reading replaces a higher one only when it is the game's own total
   *and* at least as new as the reading behind the stored value. That is the
   one way points really fall (Jagex moving a task to a cheaper tier), and the
   way a wrong stored value corrects itself without a script. A count of
   synced bits never lowers anything, so a lagging registry or a zero read at
   login cannot take points away.
3. An equal reading only moves the observation time forward, which is what
   stops an in-game total that arrives late from lowering a newer reading.

The rule runs as one conditional UPDATE rather than a read and then a write,
so a sync and a completion for the same player landing together cannot
interleave and lose the newer reading, and nothing takes ``FOR UPDATE``.

The UPDATE's row lock still lasts until the caller commits, which is why
callers on the webhook consumer must **commit before their next await**. Its
six workers share one event loop and block it on every DB call, so a worker
that yields while holding this lock can be waited on by a sibling that is
blocking the very loop the holder needs to commit: the 2026-09-03 KC
milestone stall, 30 s at a time.

Stdlib + SQLAlchemy core only, so the rule can be exercised against SQLite in
the unit tests.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import text

SOURCE_GAME = "game"
SOURCE_SYNC = "sync"
SOURCES = (SOURCE_GAME, SOURCE_SYNC)

# Far above any real account (every task in the game is worth 2,697 points as
# of 2026-09) and far below anything a column cannot hold. A reading outside
# it is a broken or hostile client, not a player.
MAX_POINTS = 10_000

_REGISTRY_KEY = "combat_achievement_tasks"
_REGISTRY_TTL_SECONDS = 300
_registry_cache: Optional[tuple] = None


def valid_points(value: Any) -> Optional[int]:
    """``value`` as a storable total, or None. Bools are rejected — ``True`` is
    an int in Python and would be stored as one point."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int):
        return None
    return value if 0 <= value <= MAX_POINTS else None


def record_ca_points(session, player_id: int, points: Any, source: str,
                     observed_at: datetime, *, authoritative: Optional[bool] = None,
                     now: Optional[datetime] = None) -> bool:
    """Apply one reading of a player's total; True if the stored row changed.

    Creates the row when the player has none — a completion from a player who
    has never synced — with ``varps`` NULL, which readers take to mean "no
    bits", not "nothing completed".

    ``authoritative`` is whether a lower reading may replace a higher one when
    it is newer (rule 2). It defaults to "the game said so"; the backfill
    passes False for totals recovered from notification history, whose
    timestamps are processing times rather than when the game was read.

    Runs on the caller's session and does not commit; commit before the next
    await (see the module docstring).
    """
    if source not in SOURCES:
        raise ValueError(f"unknown combat achievement points source {source!r}")
    points = valid_points(points)
    if points is None or player_id is None or observed_at is None:
        return False
    if authoritative is None:
        authoritative = source == SOURCE_GAME

    params = {
        "player_id": int(player_id),
        "points": points,
        "source": source,
        "observed_at": observed_at,
        "now": now or datetime.now(),
    }
    session.execute(text(_insert_missing_sql(_dialect_name(session))), params)
    result = session.execute(text(_update_sql(authoritative)), params)
    return bool(getattr(result, "rowcount", 0))


def _dialect_name(session) -> str:
    try:
        return session.get_bind().dialect.name
    except Exception:
        return "mysql"


def _insert_missing_sql(dialect: str) -> str:
    """A points-only row for a player who has none; a no-op otherwise."""
    insert = (
        "INSERT INTO player_ca_varps (player_id, varps, updated_at) "
        "VALUES (:player_id, NULL, :now) "
    )
    if dialect == "sqlite":
        return insert + "ON CONFLICT(player_id) DO NOTHING"
    # Not INSERT IGNORE: that would also swallow a foreign-key failure.
    return insert + "ON DUPLICATE KEY UPDATE player_id = player_id"


def _update_sql(authoritative: bool) -> str:
    """The keep-or-replace rule (see the module docstring) as one UPDATE.

    ``updated_at`` is assigned first on purpose: MySQL evaluates SET clauses
    left to right against the values already assigned, so it has to read
    ``points`` before ``points`` is overwritten. It moves only when the total
    does, so it keeps meaning "the combat achievement data last changed".
    """
    lower = ""
    if authoritative:
        lower = (
            "\n     OR (:points < points"
            " AND (points_observed_at IS NULL OR points_observed_at <= :observed_at))"
        )
    return f"""
        UPDATE player_ca_varps
        SET updated_at = CASE WHEN points IS NULL OR points <> :points
                              THEN :now ELSE updated_at END,
            points = :points,
            points_source = :source,
            points_observed_at = CASE
                WHEN points_observed_at IS NULL OR points_observed_at < :observed_at
                THEN :observed_at ELSE points_observed_at END
        WHERE player_id = :player_id
          AND (
                points IS NULL
             OR :points > points
             OR (:points = points
                 AND (points_observed_at IS NULL OR points_observed_at < :observed_at)){lower}
          )
    """


def load_task_registry(session) -> List[dict]:
    """The combat achievement task registry from the manifest, cached.

    Each task carries the varp and bit that record it and its tier. An empty or
    unreadable answer is returned but never cached: the web API once cached an
    empty collection log structure permanently and rendered "nothing recorded"
    over data that was sitting in the database until the process restarted.
    """
    global _registry_cache
    now = time.monotonic()
    if _registry_cache is not None and _registry_cache[0] > now:
        return _registry_cache[1]

    row = session.execute(
        text("SELECT payload FROM plugin_manifest_sections WHERE `key` = :key"),
        {"key": _REGISTRY_KEY},
    ).first()
    tasks: List[dict] = []
    if row is not None:
        try:
            loaded = json.loads(row[0])
        except (TypeError, ValueError):
            loaded = None
        if isinstance(loaded, dict) and isinstance(loaded.get("tasks"), list):
            tasks = [t for t in loaded["tasks"] if isinstance(t, dict)]

    if tasks:
        _registry_cache = (now + _REGISTRY_TTL_SECONDS, tasks)
    return tasks


def reset_registry_cache() -> None:
    """Forget the cached registry (tests, and anything that just rebuilt it)."""
    global _registry_cache
    _registry_cache = None
