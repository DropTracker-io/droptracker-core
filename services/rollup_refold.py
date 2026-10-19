"""Which days the periodic rollup re-fold recomputes.

``services/item_totals.py`` and ``services/npc_totals.py`` re-fold (absolutely
recompute from ``drops``) their hourly rollups on a schedule driven from
``data/player_total_updater.py``. That pass used to cover every day of the
current month (plus the previous month on days 1-3) every hour: ~1,500
statements an hour per rollup re-reading up to a month of drops, the largest
steady read load on the database.

What the pass actually heals splits by age:

* **Commit-order gaps** (the reason it exists, see ``refold_day``): a drop whose
  transaction commits after the tailer has moved past its id. Those are drops
  written moments ago, so they sit in the last day or two.
* **Corrections to older drops**: a Discord "Modify Entry" value edit or delete
  (``services/entry_modifier.py``), duplicate cleanups and other maintenance
  scripts, and the rare late replay dated days back.

So the hourly pass now recomputes only the last ``RECENT_DAYS`` days, plus any
day ``mark_dirty`` flagged -- the Discord edit/delete paths flag theirs, so
those still show on item/NPC pages within the hour. The whole-month pass still
runs, every ``FULL_INTERVAL_SEC`` (and on the first pass after a restart),
covering everything else. Setting ``ROLLUP_REFOLD_FULL_INTERVAL_SEC`` to the
hourly interval restores the old every-pass full re-fold.

Re-folding a day is idempotent, so a spurious mark only costs a recompute.
Marks are fail-open: a Redis fault never fails the edit that made it, it only
leaves that day for the next full pass.
"""

import os
from datetime import date, datetime

from utils.redis import RedisClient

RECENT_DAYS = max(1, int(os.getenv("ROLLUP_REFOLD_RECENT_DAYS", "2")))
FULL_INTERVAL_SEC = int(os.getenv("ROLLUP_REFOLD_FULL_INTERVAL_SEC", "21600"))

# One set per rollup, so each loop clears only what it has recomputed.
KINDS = ("item_totals", "npc_totals")
DIRTY_KEY = "rollup:refold:dirty:{kind}"
# A mark outlives any plausible full-pass interval; the TTL only stops the set
# growing forever if a loop is switched off.
DIRTY_TTL_SEC = 45 * 24 * 3600


def _redis():
    return RedisClient().client


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def mark_dirty(day_or_datetime) -> None:
    """Flag a day for both rollups' next re-fold. Never raises."""
    day = _as_date(day_or_datetime)
    if day is None:
        return
    try:
        client = _redis()
        for kind in KINDS:
            key = DIRTY_KEY.format(kind=kind)
            client.sadd(key, day.isoformat())
            client.expire(key, DIRTY_TTL_SEC)
    except Exception as e:
        print(f"rollup_refold: couldn't mark {day} dirty ({e}); the next full re-fold covers it")


def dirty_days(kind: str) -> set:
    """Days flagged for ``kind``. A Redis fault reads as none flagged."""
    try:
        members = _redis().smembers(DIRTY_KEY.format(kind=kind)) or set()
    except Exception as e:
        print(f"rollup_refold: couldn't read dirty days for {kind}: {e}")
        return set()
    days = set()
    for raw in members:
        try:
            days.add(date.fromisoformat(raw.decode() if isinstance(raw, bytes) else str(raw)))
        except ValueError:
            continue
    return days


def unmark(kind: str, day: date) -> None:
    try:
        _redis().srem(DIRTY_KEY.format(kind=kind), day.isoformat())
    except Exception:
        pass


def remark(kind: str, day: date) -> None:
    """Put a mark back after its re-fold failed, so the next pass retries it."""
    try:
        client = _redis()
        key = DIRTY_KEY.format(kind=kind)
        client.sadd(key, day.isoformat())
        client.expire(key, DIRTY_TTL_SEC)
    except Exception:
        pass


def select_plan(full_plan, today: date, dirty, recent_days: int = None) -> list:
    """The entries of ``full_plan`` an hourly (non-full) pass recomputes: the
    last ``recent_days`` days up to today, and any dirty day. Days the full
    plan doesn't cover stay uncovered, exactly as before."""
    window = RECENT_DAYS if recent_days is None else max(1, int(recent_days))
    picked = []
    for entry in full_plan:
        day_start = _as_date(entry[1])
        if day_start is None:
            continue
        age = (today - day_start).days
        if 0 <= age < window or day_start in dirty:
            picked.append(entry)
    return picked
