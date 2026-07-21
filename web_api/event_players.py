"""Pure helpers for the event-wide player contribution leaderboard
(``GET /events/{id}/players`` in ``web_api/routes/events.py``).

No DB / service imports on purpose: the unit tests import this directly under
the pytest conftest stubs (which stub ``db``/``services``), the same isolation
pattern ``web_api/event_breakdown.py`` follows. All I/O (the aggregate queries
and item-name -> id resolution) stays in the route; this module only merges and
orders the already-fetched rows.
"""
from __future__ import annotations


def norm_item_name(name: str) -> str:
    """Lowercase / whitespace-collapsed item-name key — matches the resolution
    ``_attach_task_tiles`` uses so a contributed item maps to the same id/icon."""
    return " ".join((name or "").strip().lower().split())


def top_items(rows: list[dict], item_ids: dict, limit: int) -> list[dict]:
    """Order one player's contributed-item rows (``{name, quantity, drops}``) by
    quantity desc, attach ``item_id`` from a ``{normalized name -> id}`` map
    (``None`` when unknown), and keep the top ``limit``."""
    ordered = sorted(rows, key=lambda r: (-int(r.get("quantity") or 0),
                                          -int(r.get("drops") or 0),
                                          (r.get("name") or "").lower()))
    out = []
    for it in ordered[: max(limit, 0)]:
        out.append({**it, "item_id": item_ids.get(norm_item_name(it.get("name") or ""))})
    return out


def rank_players(contrib: dict, points: dict, membership: dict,
                 names: dict, items_by_pid: dict) -> list[dict]:
    """Merge the per-player rollups into leaderboard rows.

    Every player id present in the applied-ledger rollup (``contrib``) or the
    split-points map (``points``) becomes one row, enriched with team membership
    + name + their top contributed items. Sorted by points, then completions,
    then quantity, then name — the same contribution ordering the team roster
    uses (event-team-view.tsx).
    """
    rows = []
    for pid in set(contrib) | set(points):
        agg = contrib.get(pid) or {}
        mem = membership.get(pid) or {}
        rows.append({
            "player_id": pid,
            "player_name": names.get(pid) or f"Player {pid}",
            "team_id": mem.get("team_id"),
            "team_name": mem.get("team_name"),
            "team_color": mem.get("team_color"),
            "role": mem.get("role"),
            "points": round(float(points.get(pid, 0.0)), 2),
            "completions": int(agg.get("completions", 0)),
            "quantity": int(agg.get("quantity", 0)),
            "tasks_contributed": int(agg.get("tasks", 0)),
            "items": items_by_pid.get(pid, []),
        })
    rows.sort(key=lambda r: (-r["points"], -r["completions"], -r["quantity"],
                             (r["player_name"] or "").lower()))
    return rows
