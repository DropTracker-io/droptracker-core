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

# NOTE: services.event_engine is imported lazily inside functions (matching
# event_admin.py) — the unit-test conftest stubs `services` in sys.modules, so
# a module-level import here breaks collection of every test that imports
# web_api.routes.events.

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
    from services.event_engine import _norm

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
    excluding bonus rows (mirrors the engine rollups). Metric-path rows
    (any_path KC/GP alternatives, tagged ``note: path:N``) are excluded too —
    their quantities belong to their own path, not the item checklists, and
    a 1.2M-GP row must never read as a 1.2M-item wildcard award."""
    from services.event_engine import _norm, _row_path_idx

    qty: dict = {}
    distinct: set = set()
    wildcard = 0
    for r in rows:
        if (getattr(r, "source_type", None) or "") == _BONUS:
            continue
        if _row_path_idx(r) is not None:
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
    first. ``items`` groups by matched item name (``None`` = wildcard/manual).
    ``note`` carries the organizer's reason on manual awards (newest wins on
    the rare multi-noted rollup) so the UI never shows a bare "Manual award"
    when an explanation exists."""
    from services.event_engine import display_note

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
            rec = {"player_id": pid, "quantity": 0, "_items": {}, "_last_id": 0,
                   "_last_at": None, "_note": None, "_note_id": -1}
            agg[key] = rec
        q = max(int(getattr(r, "quantity", 1) or 1), 1)
        rec["quantity"] += q
        name = getattr(r, "matched_target", None) or None
        rec["_items"][name] = rec["_items"].get(name, 0) + q
        rid = int(getattr(r, "id", 0) or 0)
        if rid >= rec["_last_id"]:
            rec["_last_id"] = rid
            rec["_last_at"] = getattr(r, "created_at", None)
        note = display_note(getattr(r, "note", None))
        if note and rid >= rec["_note_id"]:
            rec["_note_id"] = rid
            rec["_note"] = note
    out = []
    for rec in agg.values():
        out.append({
            "player_id": rec["player_id"],
            "player_name": player_names.get(rec["player_id"]) if rec["player_id"] else None,
            "quantity": rec["quantity"],
            "items": [{"name": k, "quantity": v} for k, v in rec["_items"].items()],
            "note": rec["_note"],
            "last_at": ts(rec["_last_at"]) if rec["_last_at"] is not None else None,
        })
    out.sort(key=lambda c: (c["last_at"] or 0), reverse=True)
    return out


def build_task_breakdown(task: dict, tile: dict | None, rows, progress_row,
                         team: dict, player_names: dict, ts,
                         pending_rows=None, target_override: int | None = None) -> dict:
    """Assemble the full per-(task, team) breakdown payload.

    ``rows`` are the applied ``EventCompletion`` rows for this (task, team)
    (status in auto/confirmed/manual); ``progress_row`` is the authoritative
    ``EventProgress`` rollup (or None); ``ts`` converts a datetime to an epoch.

    ``target_override`` is the caller's team-aware effective threshold
    (whole_team pb tasks scale to the roster — the pure fallback here can't
    know the team). Callers with a session should always pass it.

    ``pending_rows`` (web53a) are the team's pending-review ledger rows: when
    present, the breakdown is built a second time with them folded in and the
    two are diffed, annotating every item/group/meter whose state the pending
    rows would change (``pending`` quantities, ``pending_satisfied`` flags)
    plus top-level ``pending_count``/``pending_complete``.
    """
    from services.event_engine import (
        _config_item_entries,
        _norm,
        _parse_requirement_groups,
        completion_threshold,
        parse_task_config,
    )

    config = parse_task_config(task.get("config"))
    kind = config.get("kind")
    ttype = task.get("type")
    target_val = (int(target_override) if target_override else
                  completion_threshold({"type": ttype, "config": config,
                                        "target_value": task.get("target_value")}))
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
        from services.event_engine import (
            PATH_METRICS,
            _is_points_path,
            _path_need,
            _path_point_weights,
            _row_path_idx,
        )

        # Metric-path rows are path-scoped (note: path:N): fold each path's
        # tagged quantities, mirroring _anypath_progress_from_rows.
        tagged: dict = {}
        for r in rows:
            p_idx = _row_path_idx(r)
            if p_idx is None or (getattr(r, "source_type", None) or "") == _BONUS:
                continue
            tagged[p_idx] = tagged.get(p_idx, 0) + max(int(getattr(r, "quantity", 1) or 1), 1)
        paths_out: list[dict] = []
        best_idx, best_pct = -1, -1
        for pi, path in enumerate(config.get("paths") or []):
            if not isinstance(path, dict):
                continue
            if path.get("metric") in PATH_METRICS:
                unit = "KC" if path["metric"] == "kc" else "GP"
                need_total = _path_need(path)
                got_total = min(tagged.get(pi, 0), need_total)
                npc_names = [str(n).strip() for n in (path.get("npcs") or [])
                             if str(n).strip()]
                entry = {
                    "label": path.get("label") or f"{need_total:,} {unit}",
                    "groups": [],
                    "need": need_total, "got": got_total,
                    "pct": int(got_total * 100 // need_total),
                    "closest": False,
                    "metric": path["metric"], "unit": unit,
                    "npcs": [{"name": n, "icon": icons.get(_norm(n))}
                             for n in npc_names],
                }
            elif _is_points_path(path):
                # Points path: a weighted item checklist raced toward a pts
                # goal (rendered as a "points" group, like a flat
                # point_collection — task-detail already handles the mode).
                need_total = _path_need(path)
                weights = _path_point_weights(path)
                pitems = [_item_row(nn, icons, qty_by, {"points": w})
                          for nn, w in weights.items()]
                got_total = min(
                    sum(w * int(qty_by.get(nn, 0)) for nn, w in weights.items()),
                    need_total)
                entry = {
                    "label": path.get("label") or f"{need_total:,} pts",
                    "groups": [{
                        "mode": "points", "need": need_total,
                        "obtained": got_total,
                        "satisfied": got_total >= need_total,
                        "unit": "pts", "items": pitems,
                    }],
                    "need": need_total, "got": got_total,
                    "pct": int(got_total * 100 // need_total) if need_total else 0,
                    "closest": False,
                }
            else:
                pgroups = []
                need_total = got_total = 0
                for mode, names, need in _parse_requirement_groups(path):
                    g = _group_from_names(mode, names, need, qty_by, icons, entries)
                    need_total += need
                    got_total += g["obtained"]
                    pgroups.append(g)
                entry = {
                    "label": path.get("label") or f"Path {len(paths_out) + 1}",
                    "groups": pgroups, "need": need_total, "got": got_total,
                    "pct": int(got_total * 100 // need_total) if need_total else 0,
                    "closest": False,
                }
            paths_out.append(entry)
            if entry["pct"] > best_pct:
                best_pct, best_idx = entry["pct"], len(paths_out) - 1
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
        # pb_target stays a binary pass/fail only in its legacy single-shot
        # form; the counted requirements (times / unique_players /
        # whole_team) meter toward their target in kills or players.
        unit = _METER_UNITS.get(ttype, "")
        binary = ttype == "skill_target"
        if ttype == "pb_target":
            from services.event_engine import _pb_mode

            mode, _need = _pb_mode({"config": config})
            binary = target_val <= 1 and mode == "times"
            if not binary:
                unit = "times" if mode == "times" else "players"
        out["structure"] = "meter"
        out["meter"] = {
            "progress": prog,
            "target": target_val,
            "unit": unit,
            "binary": binary,
            "label": task.get("target") or task.get("label"),
            "target_value": task.get("target_value"),
        }

    out["contributors"] = _contributors(rows, player_names, ts)

    if pending_rows:
        _annotate_pending(out, task, tile, rows, pending_rows, team,
                          player_names, ts, config, kind, target_val, prog,
                          completed)
    return out


def _annotate_pending(out: dict, task: dict, tile, rows, pending_rows, team,
                      player_names, ts, config, kind, target_val: int,
                      prog: int, completed: bool) -> None:
    """Fold the pending rows into a projected breakdown and diff it onto
    ``out`` (see :func:`build_task_breakdown`)."""
    from types import SimpleNamespace

    from services.event_engine import (
        _anypath_progress_from_rows,
        _distinct_players_from_rows,
        _distinct_progress_from_rows,
        _grouped_progress_from_rows,
        _pb_distinct_players,
    )

    combined = list(rows) + list(pending_rows)
    if _pb_distinct_players({"type": task.get("type"), "config": config}):
        proj_val = _distinct_players_from_rows(combined, target_val)
    elif kind in ("all_of", "assembly"):
        proj_val = _distinct_progress_from_rows(combined, target_val)
    elif kind == "groups":
        proj_val = _grouped_progress_from_rows(combined, config, target_val)
    elif kind == "any_path":
        proj_val = _anypath_progress_from_rows(combined, config, target_val)
    else:
        proj_val = prog + sum(
            max(int(getattr(r, "quantity", 1) or 1), 1) for r in pending_rows
            if (getattr(r, "source_type", None) or "") != _BONUS
        )
    projected = build_task_breakdown(
        task, tile, combined,
        SimpleNamespace(progress=proj_val, completed=False),
        team, player_names, ts, target_override=target_val,
    )

    def _diff_group(base_g: dict, proj_g: dict) -> None:
        if proj_g.get("satisfied") and not base_g.get("satisfied"):
            base_g["pending_satisfied"] = True
        for base_it, proj_it in zip(base_g.get("items") or [],
                                    proj_g.get("items") or []):
            delta = int(proj_it.get("obtained") or 0) - int(base_it.get("obtained") or 0)
            if delta > 0:
                base_it["pending"] = delta
            if proj_it.get("satisfied") and not base_it.get("satisfied"):
                base_it["pending_satisfied"] = True

    for base_g, proj_g in zip(out.get("groups") or [], projected.get("groups") or []):
        _diff_group(base_g, proj_g)
    for base_p, proj_p in zip(out.get("paths") or [], projected.get("paths") or []):
        for base_g, proj_g in zip(base_p.get("groups") or [],
                                  proj_p.get("groups") or []):
            _diff_group(base_g, proj_g)
        # Path-level overlay (metric paths have no item rows to carry it).
        delta = int(proj_p.get("got") or 0) - int(base_p.get("got") or 0)
        if delta > 0:
            base_p["pending"] = delta
        if (int(proj_p.get("pct") or 0) >= 100
                and int(base_p.get("pct") or 0) < 100):
            base_p["pending_satisfied"] = True
    if out.get("meter") and projected.get("meter"):
        delta = (int(projected["meter"].get("progress") or 0)
                 - int(out["meter"].get("progress") or 0))
        if delta > 0:
            out["meter"]["pending"] = delta

    out["pending_count"] = len(pending_rows)
    out["pending_complete"] = (not completed) and proj_val >= target_val
