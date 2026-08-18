"""Team-independent "what actually counts for this task" resolution.

``web_api/event_breakdown.py`` answers *how far one team has got*; this module
answers the question that comes first and had no surface at all: **which
items, pets or NPCs satisfy this task?** A task like "obtain any 3 pets" over
a 39-name allow-list rendered as a single line of prose — the list existed
only in the raw config JSON, so a participant could not tell a qualifying pet
from a non-qualifying one, and an organiser could not proof-read what they had
built.

Split the same way as :mod:`web_api.task_tiles`:

- :func:`requirement_spec` — task dict → unresolved groups (names only, pure)
- :func:`build_requirements` — spec + name→id maps → the serialized payload

Group shape deliberately mirrors ``TaskBreakdownGroup`` (mode / need / unit /
items) minus the per-team progress fields, so one frontend component renders
both the static requirement list and the live checklist.

The engine helpers (``_config_item_entries``, ``_parse_requirement_groups``)
are imported lazily inside functions — the unit-test conftest stubs
``services`` in ``sys.modules``, so a module-level import breaks collection.
"""

from __future__ import annotations

from web_api.task_tiles import (
    _fmt_num,
    _fmt_time,
    _norm,
    _parse_config,
    pet_collection_names,
)

# Metric task types: no item checklist, just a single target line.
_METRIC_SUMMARY = {
    "kc_target": "KC",
    "xp_target": "XP",
    "ehp_target": "EHP",
    "ehb_target": "EHB",
}


def _entry_fields(entry) -> tuple[int, object]:
    """(required quantity, points) from one config item entry."""
    required, points = 1, None
    if isinstance(entry, dict):
        if entry.get("quantity"):
            try:
                required = max(int(entry["quantity"]), 1)
            except (TypeError, ValueError):
                required = 1
        if entry.get("points") is not None:
            points = entry.get("points")
    return required, points


def _item(name: str, entry=None, required_default: int = 1) -> dict:
    required, points = _entry_fields(entry)
    if required == 1 and required_default > 1:
        required = required_default
    out = {"name": name, "required": required}
    if points is not None:
        out["points"] = points
    return out


def _group(mode: str, items: list, need: int, *, label=None, unit=None) -> dict:
    out = {"mode": mode, "need": need, "items": items}
    if label:
        out["label"] = label
    if unit:
        out["unit"] = unit
    return out


def _display_names(config: dict, norms) -> dict:
    """normalized name → the original-cased spelling as authored in the config.

    ``_config_item_entries`` keys by the normalized name, which is what the
    matcher compares — but showing a player "twisted bow" instead of "Twisted
    bow" reads like a bug, so recover the authored spelling."""
    out: dict = {}

    def _scan(seq) -> None:
        for it in seq or []:
            raw = it if isinstance(it, str) else (
                (it or {}).get("item_name") or (it or {}).get("name")
                if isinstance(it, dict) else None)
            if raw:
                out.setdefault(_norm(raw), str(raw).strip())

    _scan(config.get("items"))
    _scan(config.get("any_of"))
    for group in config.get("groups") or []:
        if isinstance(group, dict):
            _scan(group.get("items"))
    for path in config.get("paths") or []:
        if isinstance(path, dict):
            _scan(path.get("items"))
            for group in path.get("groups") or []:
                if isinstance(group, dict):
                    _scan(group.get("items"))
    return {n: out.get(n, n) for n in norms}


def _item_collection_groups(task: dict, config: dict, need: int) -> tuple[list, list]:
    """(groups, paths) for an item_collection task — the same three shapes the
    breakdown reconstructs: multi-group ``groups``, either/or ``paths``, and
    the flat any_of/all_of/assembly/point_collection list."""
    from services.event_engine import _config_item_entries, _parse_requirement_groups

    kind = config.get("kind")
    entries = _config_item_entries(config)

    def _named(norms, entries_map, required_default=1):
        names = _display_names(config, list(norms))
        return [_item(names[n], entries_map.get(n), required_default) for n in norms]

    if kind == "groups":
        return [
            _group(mode, _named(sorted(names), entries), grp_need,
                   label=("All of" if mode == "all_of" else
                          f"Any {grp_need} of" if grp_need > 1 else "Any of"))
            for mode, names, grp_need in _parse_requirement_groups(config)
        ], []

    if kind == "any_path":
        from services.event_engine import _is_points_path, _path_need, _path_point_weights

        paths = []
        for path in config.get("paths") or []:
            if not isinstance(path, dict):
                continue
            path_need = _path_need(path)
            if path.get("metric"):
                unit = "KC" if path["metric"] == "kc" else "GP"
                paths.append({
                    "label": path.get("label") or f"{_fmt_num(path_need)} {unit}",
                    "metric": path["metric"], "unit": unit, "need": path_need,
                    "npcs": [str(n).strip() for n in (path.get("npcs") or [])
                             if str(n).strip()],
                    "groups": [],
                })
            elif _is_points_path(path):
                weights = _path_point_weights(path)
                names = _display_names(config, list(weights))
                paths.append({
                    "label": path.get("label") or f"{_fmt_num(path_need)} pts",
                    "need": path_need, "npcs": [],
                    "groups": [_group("points",
                                      [_item(names[n], {"points": w})
                                       for n, w in weights.items()],
                                      path_need, label="Points", unit="pts")],
                })
            else:
                groups = [
                    _group(mode, _named(sorted(names), entries), grp_need,
                           label=("All of" if mode == "all_of" else
                                  f"Any {grp_need} of" if grp_need > 1 else "Any of"))
                    for mode, names, grp_need in _parse_requirement_groups(path)
                ]
                paths.append({
                    "label": path.get("label") or f"Path {len(paths) + 1}",
                    "need": sum(g["need"] for g in groups), "npcs": [],
                    "groups": groups,
                })
        return [], paths

    if entries:
        if kind in ("all_of", "assembly"):
            mode, label, unit = "all_of", "All of", None
        elif kind == "point_collection":
            mode, label, unit = "points", "Points", "pts"
        else:
            mode = "any_of"
            label = f"Any {need} of" if need > 1 else "Any of"
            unit = None
        # A single-item any_of ("6000× Vial of blood") is a counted goal, so
        # the group need belongs on the item row.
        single = need if mode == "any_of" and len(entries) == 1 else 1
        names = _display_names(config, list(entries))
        items = [_item(names[n], entries[n], single) for n in entries]
        group_need = len(items) if mode == "all_of" else need
        return [_group(mode, items, group_need, label=label, unit=unit)], []

    # Config-less single target: "Collect 3× Twisted bow".
    target = (task.get("target") or "").strip()
    if target:
        return [_group("count", [_item(target, None, need)], need)], []
    return [], []


def requirement_spec(task: dict) -> dict:
    """Pure: a task dict → its unresolved requirement spec.

    Returns ``{"type", "kind", "summary", "groups": [...], "paths": [...],
    "npcs": [name], "notes": [str]}``. Icon ids are the caller's job
    (:func:`build_requirements`); ``spec_requirement_names`` reports what needs
    resolving."""
    ttype = task.get("type")
    config = _parse_config(task.get("config"))
    target = (task.get("target") or "").strip()
    tv = task.get("target_value")
    try:
        need = max(int(tv), 1) if tv is not None else 1
    except (TypeError, ValueError):
        need = 1

    out: dict = {
        "type": ttype,
        "kind": config.get("kind"),
        "summary": "",
        "groups": [],
        "paths": [],
        "npcs": [],
        "notes": [],
    }

    if ttype == "pet_collection":
        names = pet_collection_names(task, config)
        if target:
            out["summary"] = (f"Obtain {need}× {names[0] if names else target}"
                              if need > 1 else f"Obtain {names[0] if names else target}")
            out["groups"] = [_group("count", [_item(names[0] if names else target,
                                                    None, need)], need)]
        else:
            label = f"Any {need} of" if need > 1 else "Any of"
            out["summary"] = (
                f"Obtain any {need} of these {len(names)} pets" if need > 1
                else f"Obtain any one of these {len(names)} pets")
            out["groups"] = [_group("any_of", [_item(n) for n in names], need,
                                    label=label)]
        # The matcher's duplicate gate is invisible in the config and is the
        # single most common "why didn't this count?" question on pet tasks.
        out["notes"].append(
            "Only pets you obtain during the event count — a duplicate of a "
            "pet the account already owns does not.")
        if not target and not config.get("pets") and not config.get("categories"):
            out["notes"].append(
                "Holiday and joke pets (the “misc” category) are excluded "
                "unless the task lists them explicitly.")
        return out

    if ttype == "item_collection":
        groups, paths = _item_collection_groups(task, config, need)
        out["groups"], out["paths"] = groups, paths
        total = sum(len(g["items"]) for g in groups) or sum(
            len(g["items"]) for p in paths for g in p["groups"])
        kind = config.get("kind")
        if paths:
            out["summary"] = "Complete any ONE of these paths"
        elif kind in ("all_of", "assembly"):
            out["summary"] = f"Collect all {total} items"
        elif kind == "point_collection":
            out["summary"] = f"Score {_fmt_num(need)} points from these {total} items"
        elif kind == "groups":
            out["summary"] = "Satisfy every requirement group"
        elif total > 1:
            out["summary"] = f"Collect any {need} from these {total} items"
        elif target:
            out["summary"] = f"Collect {need}× {target}" if need > 1 else f"Collect {target}"
        sources = [str(n).strip() for n in (config.get("source_npcs") or [])
                   if str(n).strip()]
        out["npcs"] = sources
        if sources:
            out["notes"].append(
                "Only drops from " + ", ".join(sources) + " count.")
        per_item = {k: v for k, v in (config.get("item_npcs") or {}).items() if v}
        if per_item:
            out["notes"].append(
                f"{len(per_item)} of these items only count from a specific source.")
        if config.get("pet_items"):
            out["notes"].append(
                "Pets in this list are credited from the pet drop itself, not "
                "from a collection-log entry.")
        return out

    if ttype == "loot_value":
        sources = [str(n).strip() for n in (config.get("source_npcs") or [])
                   if str(n).strip()] or ([target] if target else [])
        out["npcs"] = sources
        out["summary"] = f"Accumulate {_fmt_num(tv)} GP" + (
            " from " + ", ".join(sources) if sources else " of loot")
        return out

    if ttype == "kc_target":
        npcs = [str(n).strip() for n in (config.get("npcs") or []) if str(n).strip()]
        out["npcs"] = npcs or ([target] if target else [])
        who = " / ".join(out["npcs"]) or target
        out["summary"] = f"{_fmt_num(tv)} kills at {who}" if tv else who
        if len(out["npcs"]) > 1:
            out["notes"].append("Kills at any of these count toward the same total.")
        return out

    if ttype == "pb_target":
        out["npcs"] = [target] if target else []
        base = f"Beat {_fmt_time(tv)} at {target}" if tv else target
        mode = config.get("mode")
        if mode == "whole_team":
            base += " — every team member"
        elif mode == "unique_players":
            base += f" — {_fmt_num(config.get('need') or 1)} different players"
        elif mode == "times" and (config.get("need") or 1) != 1:
            base += f" — {_fmt_num(config.get('need'))} separate times"
        out["summary"] = base
        return out

    if ttype in ("xp_target", "skill_target"):
        out["summary"] = (f"Reach level {tv} {target}" if ttype == "skill_target"
                          else f"Gain {_fmt_num(tv)} {target} XP")
        return out

    if ttype in _METRIC_SUMMARY:
        out["summary"] = f"Reach {_fmt_num(tv)} {_METRIC_SUMMARY[ttype]}"
        return out

    out["summary"] = task.get("label") or ""
    out["notes"].append("Completed by an organiser, not automatically.")
    return out


def spec_requirement_names(spec: dict) -> tuple[set, set]:
    """(item names, npc names) a requirement spec needs resolved — normalized."""
    items: set = set()
    npcs = {_norm(n) for n in spec.get("npcs") or [] if _norm(n)}
    for group in spec.get("groups") or []:
        items |= {_norm(i["name"]) for i in group["items"] if _norm(i["name"])}
    for path in spec.get("paths") or []:
        npcs |= {_norm(n) for n in path.get("npcs") or [] if _norm(n)}
        for group in path.get("groups") or []:
            items |= {_norm(i["name"]) for i in group["items"] if _norm(i["name"])}
    return items, npcs


def build_requirements(spec: dict, item_ids: dict, npc_ids: dict) -> dict:
    """Serialize a spec into the public ``requirements`` payload, attaching an
    ``icon`` ref to every named item/NPC (``id: None`` when unresolved, so the
    frontend degrades to text rather than a broken image)."""
    def _icon_item(name: str) -> dict:
        return {"type": "item", "id": item_ids.get(_norm(name)), "name": name}

    def _icon_npc(name: str) -> dict:
        """An NPC row is ``{name, icon}`` — the same shape the breakdown's
        metric paths use, so one frontend chip renders both. (Returning a bare
        icon ref here left every NPC chip icon-less: the consumer reads
        ``.icon``, which simply wasn't there.)"""
        return {"name": name,
                "icon": {"type": "npc", "id": npc_ids.get(_norm(name)),
                         "name": name}}

    def _fill(group: dict) -> dict:
        return {**group,
                "items": [{**i, "icon": _icon_item(i["name"])} for i in group["items"]]}

    return {
        "type": spec["type"],
        "kind": spec.get("kind"),
        "summary": spec.get("summary") or "",
        "groups": [_fill(g) for g in spec.get("groups") or []],
        "paths": [{**p,
                   "groups": [_fill(g) for g in p.get("groups") or []],
                   "npcs": [_icon_npc(n) for n in p.get("npcs") or []]}
                  for p in spec.get("paths") or []],
        "npcs": [_icon_npc(n) for n in spec.get("npcs") or []],
        "notes": list(spec.get("notes") or []),
    }
