"""Bingo EHB read model — effort per player, priced into efficient hours.

``web_event_effort`` records kills at the NPCs an event's tasks make relevant,
whether or not anything dropped (see ``services/event_effort.py`` for how the
relevance set and the freeze are decided). This module turns those rows into
the shapes the event surfaces render:

- :func:`effort_by_player` — ``{player_id: summary}`` for a roster, used by the
  team detail card and the Players tab.
- :func:`effort_report` — the whole-event, admin-only participation view:
  every roster member (including ones with *no* effort at all, which is the
  point) ordered by how long they've been quiet.

Every path fails OPEN to empty/zero: effort is context, never worth a 500.
EHB pricing uses WOM's published rates, read from the cache the events worker
keeps warm — a cold cache means every figure is 0 hours, which reads as "not
known yet" rather than wrong.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, Optional


def _rates() -> dict:
    try:
        from utils.wiseoldman import get_ehb_rates_sync

        return get_ehb_rates_sync() or {}
    except Exception:
        return {}


def _rows_for(s, event_id: int, player_ids: Optional[Iterable[int]] = None) -> list:
    """Raw effort rows joined to NPC names, oldest-irrelevant ordering."""
    from db.models import EventEffort, NpcList

    q = (s.query(EventEffort.player_id, EventEffort.team_id, EventEffort.npc_id,
                 NpcList.npc_name, EventEffort.boss_metric, EventEffort.kills,
                 EventEffort.last_at, EventEffort.frozen_at, EventEffort.source)
         .outerjoin(NpcList, NpcList.npc_id == EventEffort.npc_id)
         .filter(EventEffort.event_id == event_id))
    pids = sorted({int(p) for p in (player_ids or []) if p})
    if player_ids is not None:
        if not pids:
            return []
        q = q.filter(EventEffort.player_id.in_(pids))
    return q.all()


def _group_rows(rows) -> dict:
    grouped: dict = {}
    for (player_id, _team_id, npc_id, npc_name, metric, kills,
         last_at, frozen_at, source) in rows:
        grouped.setdefault(int(player_id), []).append({
            "npc_id": int(npc_id) if npc_id is not None else None,
            "npc_name": npc_name,
            "boss_metric": metric,
            "kills": kills,
            "last_at": last_at,
            "frozen_at": frozen_at,
            "source": source,
        })
    return grouped


def _summarize(rows, rates, *, boss_limit: int) -> dict:
    from services.event_effort import rows_to_summary

    summary = rows_to_summary(rows, rates)
    last_at = summary.get("last_at")
    return {
        "ehb_hours": round(float(summary.get("ehb_hours") or 0.0), 2),
        "kills": int(summary.get("kills") or 0),
        "bosses": summary.get("bosses", [])[:boss_limit],
        "boss_count": len(summary.get("bosses") or []),
        "last_at": int(last_at.timestamp()) if isinstance(last_at, datetime) else None,
        "frozen": int(summary.get("frozen") or 0),
    }


def effort_by_player(s, event_id: int, player_ids: Iterable[int],
                     *, boss_limit: int = 8) -> Dict[int, dict]:
    """``{player_id: {ehb_hours, kills, bosses, boss_count, last_at, frozen}}``.

    Only players with recorded effort appear; callers render the rest as zero,
    the same way they already handle a member with no contributions.
    """
    pids = sorted({int(p) for p in player_ids if p})
    if not pids:
        return {}
    try:
        grouped = _group_rows(_rows_for(s, event_id, pids))
    except Exception:
        return {}
    rates = _rates()
    return {
        pid: _summarize(rows, rates, boss_limit=boss_limit)
        for pid, rows in grouped.items()
    }


def effort_report(s, event_id: int, roster: Iterable[dict],
                  *, now: Optional[datetime] = None, boss_limit: int = 5) -> dict:
    """The admin participation view.

    ``roster`` is ``[{player_id, player_name, team_id, team_name, joined_at}]``
    — passed in rather than queried here so the caller's existing roster load
    is reused. Players with no effort are INCLUDED with zeroes and a null
    ``last_at``: "who has gone quiet" is the question this answers, and the
    quietest player is precisely the one with no rows.

    Sorted by ``days_idle`` descending (never-active first), so the top of the
    list is who to chase.
    """
    now = now or datetime.now()
    try:
        grouped = _group_rows(_rows_for(s, event_id))
    except Exception:
        grouped = {}
    rates = _rates()

    players = []
    for member in roster or []:
        pid = member.get("player_id")
        if pid is None:
            continue
        rows = grouped.get(int(pid)) or []
        summary = _summarize(rows, rates, boss_limit=boss_limit)
        last_at = summary["last_at"]
        # No effort at all => idle since they joined, or since the event began.
        reference = last_at or member.get("joined_at")
        days_idle = None
        if reference is not None:
            ref_dt = (reference if isinstance(reference, datetime)
                      else datetime.fromtimestamp(int(reference)))
            days_idle = max((now - ref_dt).total_seconds() / 86400.0, 0.0)
        players.append({
            "player_id": int(pid),
            "player_name": member.get("player_name"),
            "team_id": member.get("team_id"),
            "team_name": member.get("team_name"),
            "days_idle": round(days_idle, 2) if days_idle is not None else None,
            "never_active": last_at is None,
            **summary,
        })

    players.sort(key=lambda p: (
        0 if p["never_active"] else 1,
        -(p["days_idle"] or 0.0),
        -p["ehb_hours"],
    ))
    return {
        "players": players,
        "totals": {
            "participants": len(players),
            "active": sum(1 for p in players if not p["never_active"]),
            "ehb_hours": round(sum(p["ehb_hours"] for p in players), 2),
            "kills": sum(p["kills"] for p in players),
        },
        # Surfaced so the UI can say "EHB unavailable" rather than showing a
        # confident 0 when the rate table simply hasn't been fetched yet.
        "rates_known": bool(rates),
    }
