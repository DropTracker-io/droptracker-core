"""Shadow testing for the move off Wise Old Man: flags, and the diff store.

Every consumer of player stats has one switch. It is set with
``scripts/stats_source.py`` (stored in Redis, picked up by every service
within half a minute, no restart) or pinned in the environment as
``STATS_SOURCE_<CONSUMER>``, which wins when present:

- ``wom`` (default): unchanged. WOM is read, WOM's numbers are used.
- ``shadow``: still WOM's numbers, but ours are computed beside them and the
  pair is recorded in ``stats_shadow_diffs`` for review.
- ``ours``: our numbers are used. Only ever set by the owner, one consumer at
  a time, after its shadow results have been reviewed (dev tracker t427).

Nothing here flips a switch by itself, and recording a comparison never
raises: shadow testing must not be able to hurt the path it is watching.

Consumers: ``events`` (the event reconciler), ``ehb_rates`` (boss rates used
to price effort), ``player_ehb`` (a player's EHB figure).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Iterable, List, Optional

log = logging.getLogger("stats_shadow")

WOM, SHADOW, OURS = "wom", "shadow", "ours"
CONSUMERS = ("events", "ehb_rates", "player_ehb")

# |ours - wom| at or under this is a match. Per consumer, because the numbers
# differ in kind: gained XP and KC are integers that should be equal, a rate
# is a small float, an EHB total is rounded differently by each side.
TOLERANCE = {"events": 0.0, "ehb_rates": 1e-9, "player_ehb": 0.01}


SWITCH_KEY = "stats:source:{consumer}"
_SWITCH_CACHE_SECONDS = 30.0
_switch_cache: dict = {}


def _clean(raw) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return str(raw or "").strip().strip('"').strip("'").lower()


def _stored(consumer: str) -> str:
    """The switch as set by scripts/stats_source.py, cached briefly. Any
    trouble reading it means ``wom``: the safe answer is always "unchanged"."""
    now = time.monotonic()
    cached = _switch_cache.get(consumer)
    if cached and now - cached[0] < _SWITCH_CACHE_SECONDS:
        return cached[1]
    try:
        from utils.redis import redis_client

        value = _clean(redis_client.client.get(SWITCH_KEY.format(consumer=consumer)))
    except Exception:
        value = ""
    _switch_cache[consumer] = (now, value)
    return value


def source(consumer: str) -> str:
    """The switch for one consumer: ``wom``, ``shadow`` or ``ours``."""
    raw = _clean(os.getenv(f"STATS_SOURCE_{consumer.upper()}")) or _stored(consumer)
    return raw if raw in (WOM, SHADOW, OURS) else WOM


def is_match(consumer: str, wom_value: Optional[float], our_value: Optional[float]) -> Optional[bool]:
    """True/False, or None when either side had no number to compare."""
    if wom_value is None or our_value is None:
        return None
    return abs(float(our_value) - float(wom_value)) <= TOLERANCE.get(consumer, 0.0)


def entry(metric: str, wom_value, our_value, *, player_id: int = 0, our_source: Optional[str] = None,
          our_age_seconds: Optional[int] = None, detail: Optional[str] = None) -> dict:
    return {"metric": str(metric)[:64], "wom_value": wom_value, "our_value": our_value,
            "player_id": int(player_id or 0), "our_source": our_source,
            "our_age_seconds": our_age_seconds, "detail": (detail or None) and str(detail)[:255]}


def record(db_session, consumer: str, context: str, entries: Iterable[dict],
           now: Optional[datetime] = None) -> int:
    """Store comparisons, one row per (consumer, context, player, metric),
    updating a row that already exists. The caller commits. Returns rows touched."""
    from db.models import StatsShadowDiff

    entries = list(entries)
    if not entries:
        return 0
    now = (now or datetime.now()).replace(microsecond=0)
    context = str(context)[:64]
    existing = {
        (row.player_id, row.metric): row
        for row in db_session.query(StatsShadowDiff).filter(
            StatsShadowDiff.consumer == consumer, StatsShadowDiff.context == context,
            StatsShadowDiff.player_id.in_({e["player_id"] for e in entries}))
    }
    for e in entries:
        delta = None
        if e["wom_value"] is not None and e["our_value"] is not None:
            delta = abs(float(e["our_value"]) - float(e["wom_value"]))
        row = existing.get((e["player_id"], e["metric"]))
        if row is None:
            row = StatsShadowDiff(consumer=consumer, context=context, player_id=e["player_id"],
                                  metric=e["metric"], observations=0, first_seen=now)
            db_session.add(row)
            existing[(e["player_id"], e["metric"])] = row
        row.wom_value, row.our_value = e["wom_value"], e["our_value"]
        row.our_source, row.our_age_seconds, row.detail = e["our_source"], e["our_age_seconds"], e["detail"]
        row.observations = (row.observations or 0) + 1
        if delta is not None and (row.worst_delta is None or delta > row.worst_delta):
            row.worst_delta = delta
        row.last_seen = now
    db_session.flush()
    return len(entries)


def record_safely(consumer: str, context: str, entries: Iterable[dict]) -> int:
    """`record` in its own short session. Never raises; returns 0 on failure
    (the table not migrated yet, a deadlock): a comparison is never worth an
    error in the path being compared."""
    try:
        from db.models.base import Session
    except Exception:
        return 0
    session = Session()
    try:
        count = record(session, consumer, context, entries)
        session.commit()
        return count
    except Exception as exc:
        session.rollback()
        log.warning("shadow record failed (%s %s): %s", consumer, context, exc)
        return 0
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Comparisons that are pure enough to live here
# --------------------------------------------------------------------------- #

def compare_ehb_rates(wom_rates: dict, our_rates: dict) -> List[dict]:
    """One entry per boss either side prices. A boss only one side knows has
    None on the other, which reads as "no data", not as zero."""
    return [entry(boss, wom_rates.get(boss), our_rates.get(boss))
            for boss in sorted(set(wom_rates) | set(our_rates))]


def compare_gained_rows(wom_rows_by_player: dict, our_rows_by_player: dict,
                        metrics: Iterable[str]) -> List[dict]:
    """Event shadow: what each participant gained on each relevant metric.

    Both inputs are ``{player_id: {metric: {"start", "end", "gained", ...}}}``.
    A player only WOM has is recorded with our side empty (a coverage gap); a
    player only we have is not recorded, because the live path would not have
    credited them either.
    """
    out = []
    metrics = list(metrics)
    for player_id, wom_metrics in wom_rows_by_player.items():
        ours = our_rows_by_player.get(player_id) or {}
        for metric in metrics:
            wom = wom_metrics.get(metric)
            if not wom:
                continue
            mine = ours.get(metric)
            wom_gain = wom.get("gained")
            our_gain = mine.get("gained") if mine else None
            detail = f"wom {wom.get('start')}->{wom.get('end')}"
            if mine:
                detail += f"; ours {mine.get('start')}->{mine.get('end')}"
                if mine.get("partial"):
                    detail += " (our history starts inside the window)"
            origin = (mine or {}).get("source")
            if origin and mine.get("partial"):
                # Not a fair comparison: we were not watching when the window
                # opened. The report counts these apart from real differences.
                origin = f"{origin}/partial"
            out.append(entry(metric, wom_gain, our_gain, player_id=player_id,
                             our_source=origin,
                             our_age_seconds=(mine or {}).get("age_seconds"), detail=detail))
    return out
