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


# --- Contributions ----------------------------------------------------------
# A "contribution" is one act of pushing a task forward, which is NOT the same
# as one applied ledger row.
#
# Tasks that track a rising METRIC (xp gained, kills, GP looted, a level) write
# a ledger row per update: 50 Vorkath kills is 50 rows, and an xp task collects
# a row every time the plugin reports a delta. To a player that is one ongoing
# contribution to one task, so those rows collapse to a single contribution per
# (player, task).
#
# Tasks that credit a discrete ACQUISITION — an item, a pet, a personal best —
# keep one contribution per row: pulling two of the required items on two
# different nights really is two separate contributions.
METRIC_TASK_TYPES = frozenset({
    "xp_target",
    "kc_target",
    "skill_target",
    "loot_value",
    "ehp_target",
    "ehb_target",
})


def is_metric_task(task_type) -> bool:
    """True when repeated ledger rows on this task type are progress updates
    toward one number rather than separate acquisitions."""
    return str(task_type or "") in METRIC_TASK_TYPES


def task_contributions(task_type, rows: int) -> int:
    """Contributions represented by ``rows`` applied ledger rows on ONE task by
    ONE player — the whole run collapses to 1 for metric tasks."""
    rows = int(rows or 0)
    if rows <= 0:
        return 0
    return 1 if is_metric_task(task_type) else rows


def count_contributions(rows_by_task: dict, task_types: dict) -> int:
    """Total contributions for one player from ``{task_id: ledger row count}``
    and a ``{task_id: task type}`` lookup (unknown types count per row)."""
    return sum(
        task_contributions(task_types.get(tid), n) for tid, n in rows_by_task.items()
    )


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
                 names: dict, items_by_pid: dict,
                 loot_gp: dict | None = None) -> list[dict]:
    """Merge the per-player rollups into leaderboard rows.

    Every player id present in the applied-ledger rollup (``contrib``), the
    split-points map (``points``), or the roster (``membership``) becomes one
    row — rostered players with zero contributions still appear (they carry
    the event-window loot GP figure). Enriched with team membership + name +
    top contributed items + ``loot_gp`` (raw int; the route wraps it in the
    Money envelope). Sorted by points, then completions, then quantity, then
    loot GP, then name — the same contribution ordering the team roster uses
    (event-team-view.tsx), with GP ordering the otherwise-tied tail.
    """
    loot_gp = loot_gp or {}
    rows = []
    for pid in set(contrib) | set(points) | set(membership):
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
            "loot_gp": int(loot_gp.get(pid, 0)),
            "items": items_by_pid.get(pid, []),
        })
    rows.sort(key=lambda r: (-r["points"], -r["completions"], -r["quantity"],
                             -r["loot_gp"], (r["player_name"] or "").lower()))
    return rows
