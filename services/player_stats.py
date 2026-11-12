"""Player stats from our own store, in the shapes Wise Old Man answers in.

The consumers that are moving off WOM were written against its bulk routes,
so this module answers the same questions from ``player_stat_snapshots`` in
the same row shape. A consumer can then be compared with WOM (shadow) or
switched (ours) without being rewritten.

Rule that matters everywhere here: a metric we have no reading for is left
out. It is unknown, and unknown is never reported as zero.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List, Optional

# Our skill keys -> WOM's metric slugs (boss and activity slugs are shared).
_WOM_SKILL = {"runecraft": "runecrafting"}


def wom_metric(skill: str) -> str:
    return _WOM_SKILL.get(skill, skill)


def _flat(values: Optional[dict]) -> Dict[str, int]:
    """``{wom metric slug: value}`` over skills and activities."""
    if not values:
        return {}
    out = {wom_metric(skill): xp for skill, xp in values["skills"].items()}
    out.update(values["activities"])
    return out


def gained(db_session, player_id: int, start: datetime, end: datetime) -> Dict[str, dict]:
    """``{metric: {"start", "end", "gained", "partial", "source", "age_seconds"}}``
    for one player over a window.

    The baseline is the player's stats at ``start``. When their history only
    begins inside the window the first reading in it stands in and the metric
    is marked ``partial`` (WOM does the same: its "start" is the first
    snapshot of the period). A metric with no baseline at all is left out.
    """
    from utils.stat_snapshots import first_values_between, values_at

    closing = values_at(db_session, player_id, end)
    if closing is None:
        return {}
    opening = values_at(db_session, player_id, start)
    partial = False
    if opening is None:
        opening = first_values_between(db_session, player_id, start, end)
        partial = True
    if opening is None:
        return {}
    first, last = _flat(opening), _flat(closing)
    age = max(0, int((end - closing["taken_at"]).total_seconds()))
    out = {}
    for metric, value in last.items():
        base = first.get(metric)
        if base is None:
            continue
        out[metric] = {"start": base, "end": value, "gained": max(0, value - base),
                       "partial": partial, "source": closing["source"], "age_seconds": age,
                       "taken_at": closing["taken_at"]}
    return out


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def bulk_gained_rows(db_session, players: Iterable[dict], start: datetime, end: datetime) -> List[dict]:
    """Rows shaped like WOM's ``GET /groups/:id/bulk-gained``:
    ``{"player": {"id", "username", "updatedAt"}, "startDate", "endDate",
    "data": [{"metric", "start", "end", "gained"}]}``.

    ``players`` are ``{"player_id", "name", "wom_id"}``. A player we have no
    usable history for gets no row, exactly as WOM omits a player it has no
    snapshots for. ``player.id`` is the WOM id, which is how the event
    reconciler recognises a participant.
    """
    rows = []
    for player in players:
        metrics = gained(db_session, player["player_id"], start, end)
        if not metrics:
            continue
        taken_at = next(iter(metrics.values()))["taken_at"]
        rows.append({
            "player": {"id": player.get("wom_id"), "username": player.get("name"),
                       "displayName": player.get("name"), "updatedAt": _iso(taken_at)},
            "startDate": _iso(start), "endDate": _iso(taken_at),
            "data": [{"metric": metric, "start": m["start"], "end": m["end"], "gained": m["gained"]}
                     for metric, m in sorted(metrics.items())],
            "dt_player_id": player["player_id"],
        })
    return rows
