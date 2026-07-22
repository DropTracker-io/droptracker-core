"""Event-window loot GP — a player's TOTAL tracked loot during an event.

Product decision (2026-07-22): the "GP earned" figures on the public event
Players/Teams surfaces are each player's total tracked loot between the event
going live and now (or the event's end) — from EVERY source, not only drops
that credited event tasks. Task-credited items remain the separate
``matched_target`` ledger aggregation ("what earned the points").

Window rules (:func:`event_window`, pure — unit-tested under the conftest
stubs like ``event_players``):
- start = ``activated_at`` (when tracking actually began), falling back to
  ``starts_at`` only for non-draft legacy rows that predate activation
  stamps. A draft has no window.
- end = ``ended_at`` / ``ends_at``, clamped to *now*. Future starts and
  empty/inverted windows resolve to ``None`` → every GP figure reads 0.

The rollup reads ``player_npc_hourly_totals`` (services/npc_totals.py keeps
it current to the hour; every drop carries an npc_id so it covers all loot),
NOT the raw ``drops`` table — a 2,000-player CvC roster aggregates in ~0.5s
vs ~19s against ``drops``. Granularity is the HOUR: the window's start/end
are widened to their containing hours, so figures can include up to 59
minutes of loot just before activation — fine for a decorative stat. The
per-event map is cached in Redis (60s live / 1h ended) behind the 15s HTTP
cache. Every failure path fails OPEN to zeros: GP is decorative context,
never worth a 500.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Dict, Iterable, Optional, Tuple

_CACHE_VERSION = "v2"  # v2: hourly-rollup source (was raw drops)
_TTL_LIVE = 60          # seconds — event still running, totals move
_TTL_ENDED = 3600       # seconds — window is closed, totals are final


def event_window(ev, now: Optional[datetime] = None) -> Optional[Tuple[datetime, datetime]]:
    """The [start, end] loot-counting window for an event, or None when the
    event hasn't run (drafts, future starts, inverted/empty windows)."""
    now = now or datetime.now()
    start = getattr(ev, "activated_at", None)
    if start is None and getattr(ev, "status", None) != "draft":
        start = getattr(ev, "starts_at", None)
    if start is None or start > now:
        return None
    end = getattr(ev, "ended_at", None) or getattr(ev, "ends_at", None)
    if end is None or end > now:
        end = now
    if end <= start:
        return None
    return start, end


def hour_range(window: Tuple[datetime, datetime]) -> Tuple[str, str]:
    """The inclusive ``date_hour`` string bounds (``YYYY-MM-DD-HH``, the
    player_npc_hourly_totals key format — zero-padded, so lexicographic
    comparison is chronological) containing a datetime window."""
    start, end = window
    return start.strftime("%Y-%m-%d-%H"), end.strftime("%Y-%m-%d-%H")


def covers(cached: Optional[dict], pids: Iterable[int]) -> bool:
    """True when a cached ``{pid: gp}`` map already answers every requested
    player id (players who simply had no drops are stored as explicit 0s, so
    absence really means "not computed yet" — e.g. a roster join mid-TTL)."""
    if not isinstance(cached, dict):
        return False
    return all(str(pid) in cached for pid in pids)


def _redis():
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def loot_gp_by_player(s, ev, player_ids: Iterable[int]) -> Dict[int, int]:
    """``{player_id: total loot GP}`` over the event's window for the given
    players. Cached per event in Redis; fails open to an empty map (callers
    render 0s)."""
    pids = sorted({int(p) for p in player_ids if p})
    if not pids:
        return {}
    window = event_window(ev)
    if window is None:
        return {pid: 0 for pid in pids}

    key = f"event:{getattr(ev, 'id', 0)}:lootgp:{_CACHE_VERSION}"
    r = _redis()
    if r is not None:
        try:
            raw = r.get(key)
            cached = json.loads(raw) if raw else None
            if covers(cached, pids):
                return {pid: int(cached[str(pid)]) for pid in pids}
        except Exception:
            pass

    lo, hi = hour_range(window)
    out = {pid: 0 for pid in pids}
    try:
        from sqlalchemy import func

        from db.models import PlayerNpcHourlyTotals as H

        for pid, total in (
            s.query(H.player_id, func.sum(H.total_value))
            .filter(
                H.player_id.in_(pids),
                H.date_hour >= lo,
                H.date_hour <= hi,
            )
            .group_by(H.player_id)
            .all()
        ):
            out[int(pid)] = int(total or 0)
    except Exception:
        # Fail open: a rollup error must not take down the event pages.
        return {pid: 0 for pid in pids}

    if r is not None:
        try:
            ttl = _TTL_ENDED if getattr(ev, "status", None) == "past" else _TTL_LIVE
            r.setex(key, ttl, json.dumps({str(p): v for p, v in out.items()}))
        except Exception:
            pass
    return out
