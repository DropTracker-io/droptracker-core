"""Per-(task, team) item-level progress breakdown for the event task detail UI.

``EventProgress`` collapses each task to a single integer + a done flag; the
member-facing "what's obtained / what's left" view needs the item-level truth
that only survives in the ``EventCompletion`` ledger (``matched_target`` +
``quantity`` per applied row). This module reconstructs that view by grouping
the applied ledger against the task's requirement ``config``.

It deliberately reuses the exact engine helpers (:func:`_norm`,
:func:`_parse_requirement_groups`, :func:`completion_threshold`,
:func:`parse_task_config`, :func:`_config_item_entries`) and the same
status/bonus-exclusion rules the rollups use, so a breakdown can never disagree
with the integer the engine wrote. Item *display* names and icons come from the
already-resolved ``tile`` block (``web_api/task_tiles.py``) — no extra queries.

Structures returned (``structure`` field):
- ``checklist`` — ``groups: [{mode, need, obtained, satisfied, unit?, items[]}]``
  where each item is ``{name, icon?, required, obtained, satisfied, points?}``.
  Covers flat ``any_of``/``all_of``/``assembly``/``point_collection``,
  multi-group ``groups`` configs, and config-less single-target collections.
- ``paths`` — ``paths: [{label, closest, pct, need, got, groups[]}]`` for
  ``any_path`` (dryness-protection either/or tasks).
- ``meter`` — ``meter: {progress, target, unit, binary, label, target_value}``
  for non-item tasks (kc/xp/pb/skill/ehp/ehb/loot_value/custom).

Every breakdown also carries ``progress``/``target``/``completed``/``wildcard``
and a ``contributors`` roll-up (who obtained what).
"""
from __future__ import annotations

from services.event_engine import (
    _config_item_entries,
    _norm,
    _parse_requirement_groups,
    completion_threshold,
    parse_task_config,
)

# Manual "bonus" rows are excluded from progress rollups (they're score-only),
# so they must be excluded here too — see event_engine._distinct_progress_from_rows.
_BONUS = "bonus"

# Non-item task type -> the unit shown on its single progress meter.
_METER_UNITS = {
    "kc_target": "KC",
    "xp_target": "XP",
    "skill_target": "level",
    "ehp_target": "EHP",
    "ehb_target": "EHB",
    "loot_value": "GP",
    "pb_target": "time",
}


def _icon_lookup(tile: dict | None) -> dict:
    """normalized item/npc/skill name -> icon ref from the resolved tile block."""
    out: dict = {}
    for icon in ((tile or {}).get("icons") or []):
        key = _norm(icon.get("name"))
        if key and key not in out:
            out[key] = {
                "type": icon.get("type"),
                "id": icon.get("id"),
                "name": icon.get("name"),
            }
    return out


def _display(name_norm: str, icons: dict) -> str:
    """Original-cased display name for a normalized requirement name."""
    icon = icons.get(name_norm)
    if icon and icon.get("name"):
        return icon["name"]
    return name_norm


def _ledger_stats(rows) -> tuple[dict, set, int]:
    """(qty_by_norm_name, distinct_norm_names, wildcard_qty) from applied rows,
    excluding bonus rows (mirrors the engine rollups)."""
    qty: dict = {}
    distinct: set = set()
    wildcard = 0
    for r in rows:
        if (getattr(r, "source_type", None) or "") == _BONUS:
            continue
        name = _norm(getattr(r, "matched_target", None))
        q = max(int(getattr(r, "quantity", 1) or 1), 1)
        if name:
            qty[name] = qty.get(name, 0) + q
            distinct.add(name)
        else:
            wildcard += q
    return qty, distinct, wildcard


def _item_row(name_norm: str, icons: dict, qty_by: dict, entry, required_default: int = 1) -> dict:
    required = required_default
    points = None
    if isinstance(entry, dict):
        if entry.get("quantity"):
            try:
                required = max(int(entry["quantity"]), 1)
            except (TypeError, ValueError):
                required = required_default
        if entry.get("points") is not None:
            points = entry.get("points")
    obtained = int(qty_by.get(name_norm, 0))
    row = {
        "name": _display(name_norm, icons),
        "icon": icons.get(name_norm),
        "required": required,
        "obtained": obtained,
        "satisfied": obtained >= required,
    }
    if points is not None:
        row["points"] = points
    return row


def _group_from_names(mode: str, names_norm, need: int, qty_by: dict,
                      icons: dict, entries: dict) -> dict:
    """Build one requirement group ({mode, need, obtained, satisfied, items})
    from a set/list of normalized item names."""
    items = [_item_row(nn, icons, qty_by, (entries or {}).get(nn)) for nn in names_norm]
    if mode == "all_of":
        got = sum(1 for it in items if it["satisfied"])
    else:  # any_of: quantities fold, capped at need (matches _grouped_progress_from_rows)
        got = min(sum(min(it["obtained"], it["required"]) for it in items), need)
    return {
        "mode": mode,
        "need": need,
        "obtained": min(got, need),
        "satisfied": got >= need,
        "items": items,
    }


def _contributors(rows, player_names: dict, ts) -> list[dict]:
    """Per-player rollup of applied contributions: who obtained what, newest
    first. ``items`` groups by matched item name (``None`` = wildcard/manual)."""
    agg: dict = {}
    order = 0
    for r in rows:
        if (getattr(r, "source_type", None) or "") == _BONUS:
            continue
        pid = getattr(r, "player_id", None)
        key = pid if pid is not None else f"_anon_{order}"
        order += 1
        rec = agg.get(key)
        if rec is None:
            rec = {"player_id": pid, "quantity": 0, "_items": {}, "_last_id": 0, "_last_at": None}
            agg[key] = rec
        q = max(int(getattr(r, "quantity", 1) or 1), 1)
        rec["quantity"] += q
        name = getattr(r, "matched_target", None) or None
        rec["_items"][name] = rec["_items"].get(name, 0) + q
        rid = int(getattr(r, "id", 0) or 0)
        if rid >= rec["_last_id"]:
            rec["_last_id"] = rid
            rec["_last_at"] = getattr(r, "created_at", None)
    out = []
    for rec in agg.values():
        out.append({
            "player_id": rec["player_id"],
            "player_name": player_names.get(rec["player_id"]) if rec["player_id"] else None,
            "quantity": rec["quantity"],
            "items": [{"name": k, "quantity": v} for k, v in rec["_items"].items()],
            "last_at": ts(rec["_last_at"]) if rec["_last_at"] is not None else None,
        })
    out.sort(key=lambda c: (c["last_at"] or 0), reverse=True)
    return out


def build_task_breakdown(task: dict, tile: dict | None, rows, progress_row,
                         team: dict, player_names: dict, ts) -> dict:
    """Assemble the full per-(task, team) breakdown payload.

    ``rows`` are the applied ``EventCompletion`` rows for this (task, team)
    (status in auto/confirmed/manual); ``progress_row`` is the authoritative
    ``EventProgress`` rollup (or None); ``ts`` converts a datetime to an epoch.
    """
    config = parse_task_config(task.get("config"))
    kind = config.get("kind")
    ttype = task.get("type")
    target_val = completion_threshold({"type": ttype, "target_value": task.get("target_value")})
    icons = _icon_lookup(tile)
    qty_by, _distinct, wildcard = _ledger_stats(rows)

    prog = int(getattr(progress_row, "progress", 0) or 0) if progress_row is not None else 0
    completed = bool(getattr(progress_row, "completed", False)) if progress_row is not None else False

    out: dict = {
        "task_id": task["id"],
        "team_id": team["id"],
        "team_name": team.get("name"),
        "type": ttype,
        "kind": kind,
        "progress": prog,
        "target": target_val,
        "completed": completed,
        "wildcard": wildcard,
    }

    entries = _config_item_entries(config)
    is_item_task = ttype == "item_collection"

    if is_item_task and kind == "groups":
        out["structure"] = "checklist"
        out["groups"] = [
            _group_from_names(mode, names, need, qty_by, icons, entries)
            for mode, names, need in _parse_requirement_groups(config)
        ]

    elif is_item_task and kind == "any_path":
        paths_out: list[dict] = []
        best_idx, best_pct = -1, -1
        for pi, path in enumerate(config.get("paths") or []):
            if not isinstance(path, dict):
                continue
            pgroups = []
            need_total = got_total = 0
            for mode, names, need in _parse_requirement_groups(path):
                g = _group_from_names(mode, names, need, qty_by, icons, entries)
                need_total += need
                got_total += g["obtained"]
                pgroups.append(g)
            pct = int(got_total * 100 // need_total) if need_total else 0
            paths_out.append({
                "label": path.get("label") or f"Path {len(paths_out) + 1}",
                "groups": pgroups, "need": need_total, "got": got_total,
                "pct": pct, "closest": False,
            })
            if pct > best_pct:
                best_pct, best_idx = pct, len(paths_out) - 1
        if 0 <= best_idx < len(paths_out):
            paths_out[best_idx]["closest"] = True
        out["structure"] = "paths"
        out["paths"] = paths_out

    elif is_item_task and entries:
        # Flat any_of / all_of / assembly / point_collection — one group.
        if kind in ("all_of", "assembly"):
            mode = "all_of"
        elif kind == "point_collection":
            mode = "points"
        else:
            mode = "any_of"
        items = [_item_row(nn, icons, qty_by, entries.get(nn)) for nn in entries.keys()]
        need = target_val
        if mode == "all_of":
            got = sum(1 for it in items if it["satisfied"])
        elif mode == "points":
            got = prog  # authoritative weighted-points total from the rollup
        else:
            got = min(sum(min(it["obtained"], it["required"]) for it in items), need)
        group = {
            "mode": mode,
            "need": need,
            "obtained": got if mode == "points" else min(got, need),
            "satisfied": completed or got >= need,
            "items": items,
        }
        if mode == "points":
            group["unit"] = "pts"
        out["structure"] = "checklist"
        out["groups"] = [group]

    elif is_item_task:
        # Config-less single-target collection ("collect N× item"): running count.
        tgt = (task.get("target") or "").strip()
        nn = _norm(tgt)
        item = {
            "name": tgt or task.get("label"),
            "icon": icons.get(nn),
            "required": target_val,
            "obtained": prog,
            "satisfied": completed or prog >= target_val,
        }
        out["structure"] = "checklist"
        out["groups"] = [{
            "mode": "count", "need": target_val,
            "obtained": min(prog, target_val),
            "satisfied": completed or prog >= target_val,
            "items": [item],
        }]

    else:
        # Non-item task: a single progress meter (no per-item checklist).
        out["structure"] = "meter"
        out["meter"] = {
            "progress": prog,
            "target": target_val,
            "unit": _METER_UNITS.get(ttype, ""),
            "binary": ttype in ("pb_target", "skill_target"),
            "label": task.get("target") or task.get("label"),
            "target_value": task.get("target_value"),
        }

    out["contributors"] = _contributors(rows, player_names, ts)
    return out
