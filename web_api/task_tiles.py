"""Task tile metadata for event boards.

Resurrects the legacy bingo tile system (``events/generators/BingoBoardGen.py``
+ ``/api/task_tile``, deleted in the 293c18b refactor) as *data* instead of
server-rendered PNGs: each event task gets a ``tile`` block — resolved icon
refs (itemdb / npcdb / metrics), a badge in the legacy style ("KC TARGET",
"FULL SET", …) and a short value string — and the web frontend composes the
actual tile art from the same ``/img`` assets the old PIL renderer used.

Split in two so the interesting part stays pure and unit-testable:

- :func:`tile_spec` — task dict → unresolved spec (names only, no I/O)
- :func:`build_tile` — spec + name→id maps → the serialized ``tile`` block

The route layer collects every name across an event's tasks, resolves them in
two bulk queries (see ``_attach_task_tiles`` in ``web_api/routes/events.py``),
and calls :func:`build_tile` per task.
"""

from __future__ import annotations

import json

# Icon cap per tile — beyond this the frontend shows a "+N" chip.
MAX_TILE_ICONS = 12

# The classic coin-stack icon the legacy renderer used for unscoped
# loot_value tasks (static/assets/img/itemdb/1004.png).
COINS_ITEM_ID = 1004


def _norm(value) -> str:
    """Normalize a name for case-insensitive joining (mirrors event_engine)."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _fmt_num(number) -> str:
    """Abbreviate like the legacy ``utils.format.format_number`` (100.00M)."""
    try:
        n = int(float(number or 0))
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _fmt_time(seconds) -> str:
    """Seconds → "m:ss" (or "h:mm:ss"), matching the frontend formatter."""
    try:
        total = max(int(seconds or 0), 0)
    except (TypeError, ValueError):
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _parse_config(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _config_items(config: dict) -> list[dict]:
    """Ordered, de-duplicated item entries (``{"name", "quantity"?}``) from an
    item_collection config — ``items``, bare ``any_of`` lists, and
    ``groups[].items`` sub-requirements all fold in."""
    out: list[dict] = []
    seen: set[str] = set()

    def _add(entry) -> None:
        if isinstance(entry, str):
            name, qty = entry, None
        elif isinstance(entry, dict):
            name = entry.get("item_name") or entry.get("name")
            qty = entry.get("quantity")
        else:
            return
        key = _norm(name)
        if not key or key in seen:
            return
        seen.add(key)
        item = {"name": str(name).strip()}
        try:
            if qty is not None and int(qty) > 1:
                item["quantity"] = int(qty)
        except (TypeError, ValueError):
            pass
        out.append(item)

    for it in config.get("items") or []:
        _add(it)
    for it in config.get("any_of") or []:
        _add(it)
    for group in config.get("groups") or []:
        if isinstance(group, dict):
            for it in group.get("items") or []:
                _add(it)
    for path in config.get("paths") or []:
        if isinstance(path, dict):
            for group in path.get("groups") or []:
                if isinstance(group, dict):
                    for it in group.get("items") or []:
                        _add(it)
            for it in path.get("items") or []:  # points path: flat weighted list
                _add(it)
    return out


def _metric_path_npcs(config: dict) -> list[str]:
    """Ordered, de-duplicated NPC names of an any_path config's metric paths
    (KC / loot-value alternatives) — they become boss icons on the tile."""
    out: list[str] = []
    seen: set[str] = set()
    for path in config.get("paths") or []:
        if not isinstance(path, dict) or not path.get("metric"):
            continue
        for name in path.get("npcs") or []:
            key = _norm(name)
            if key and key not in seen:
                seen.add(key)
                out.append(str(name).strip())
    return out


def _item_collection_spec(task: dict, config: dict) -> dict:
    items = _config_items(config)
    tv = task.get("target_value")
    metric_npcs = _metric_path_npcs(config)
    if not items and not metric_npcs:
        # Single-target collection: "Collect 3× Twisted bow".
        target = (task.get("target") or "").strip()
        item = {"name": target} if target else None
        if item and isinstance(tv, int) and tv > 1:
            item["quantity"] = tv
        return {"badge": "COLLECT", "value": None,
                "items": [item] if item else [], "npcs": [], "skills": []}

    kind = config.get("kind") or "any_of"
    badge, value = "ANY ITEM", None
    if kind == "any_of":
        if isinstance(tv, int) and tv > 1:
            badge = f"ANY {tv}"
    elif kind == "all_of":
        badge = "ALL ITEMS"
    elif kind == "assembly":
        badge = "FULL SET"
    elif kind == "point_collection":
        badge = "POINTS"
        if isinstance(tv, int) and tv > 0:
            value = f"{_fmt_num(tv)} pts"
    elif kind == "groups":
        badge = "COMBO"
    elif kind == "any_path":
        badge = "EITHER OR"
        # A GP-only path set with no item paths still deserves an icon.
        if not items and not metric_npcs:
            items = [{"name": "Coins", "item_id": COINS_ITEM_ID}]
    return {"badge": badge, "value": value, "items": items,
            "npcs": metric_npcs, "skills": []}


def tile_spec(task: dict) -> dict:
    """Pure: derive a task's unresolved tile spec.

    Returns ``{"badge", "value", "items": [{"name", "quantity"?}],
    "npcs": [name], "skills": [metric-name]}`` — names only; id resolution is
    the caller's job (:func:`build_tile`).
    """
    task_type = task.get("type")
    target = (task.get("target") or "").strip()
    tv = task.get("target_value")
    config = _parse_config(task.get("config"))
    empty: dict = {"badge": None, "value": None, "items": [], "npcs": [], "skills": []}

    if task_type == "item_collection":
        return _item_collection_spec(task, config)

    if task_type == "kc_target":
        return {**empty, "badge": "KC TARGET",
                "value": f"{_fmt_num(tv)} KC" if tv else None,
                "npcs": [target] if target else []}

    if task_type == "pb_target":
        value = f"sub {_fmt_time(tv)}" if tv else None
        # Completion requirement (config {"mode", "need"}): counted goals
        # show their multiplier / audience on the tile.
        mode = config.get("mode")
        if value and mode == "whole_team":
            value += " · whole team"
        elif value and mode == "unique_players":
            value += f" · {_fmt_num(config.get('need') or 1)} players"
        elif value and mode == "times":
            try:
                need = int(config.get("need") or 1)
            except (TypeError, ValueError):
                need = 1
            if need > 1:
                value += f" ×{need}"
        return {**empty, "badge": "KILL TIME", "value": value,
                "npcs": [target] if target else []}

    if task_type == "xp_target":
        return {**empty, "badge": "XP TARGET",
                "value": f"{_fmt_num(tv)} XP" if tv else None,
                "skills": [target.lower()] if target else []}

    if task_type == "skill_target":
        return {**empty, "badge": "SKILL LEVEL",
                "value": f"Lvl {tv}" if tv else None,
                "skills": [target.lower()] if target else []}

    if task_type == "loot_value":
        npcs: list[str] = []
        seen: set[str] = set()
        for name in list(config.get("source_npcs") or []) + ([target] if target else []):
            key = _norm(name)
            if key and key not in seen:
                seen.add(key)
                npcs.append(str(name).strip())
        spec = {**empty, "badge": "TOTAL LOOT",
                "value": f"{_fmt_num(tv)} GP" if tv else None, "npcs": npcs}
        if not npcs:
            spec["items"] = [{"name": "Coins", "item_id": COINS_ITEM_ID}]
        return spec

    if task_type == "ehp_target":
        return {**empty, "badge": "EHP TARGET",
                "value": f"{_fmt_num(tv)} EHP" if tv else None, "skills": ["ehp"]}

    if task_type == "ehb_target":
        return {**empty, "badge": "EHB TARGET",
                "value": f"{_fmt_num(tv)} EHB" if tv else None, "skills": ["ehb"]}

    # custom / unknown: text-only tile.
    return {**empty, "badge": "CUSTOM"}


def spec_names(spec: dict) -> tuple[set[str], set[str]]:
    """(item names, npc names) a spec needs resolved — normalized keys."""
    items = {_norm(i["name"]) for i in spec["items"]
             if i.get("name") and "item_id" not in i}
    npcs = {_norm(n) for n in spec["npcs"] if _norm(n)}
    return items, npcs


def icon_asset_path(icon: dict) -> str | None:
    """Relative ``/img`` asset path for one resolved tile icon, or ``None`` when
    it can't map to an image (unresolved item/npc id, or a nameless skill).

    Mirrors the frontend's tile art sources: items ``itemdb/{id}.png``, NPCs
    ``npcdb/{id}.png``, skills/metrics ``metrics/{name}.png`` (lowercased,
    spaces → underscores). Callers prepend the host and — since the same box
    serves ``/img`` — should confirm the file exists before using the URL, so a
    missing asset degrades to no icon rather than a broken Discord thumbnail.
    """
    kind = icon.get("type")
    if kind == "item" and icon.get("id"):
        return f"itemdb/{int(icon['id'])}.png"
    if kind == "npc" and icon.get("id"):
        return f"npcdb/{int(icon['id'])}.png"
    if kind == "skill" and icon.get("name"):
        return f"metrics/{_norm(icon['name']).replace(' ', '_')}.png"
    return None


def build_tile(spec: dict, item_ids: dict[str, int], npc_ids: dict[str, int]) -> dict:
    """Serialize a spec into the public ``tile`` block.

    ``item_ids`` / ``npc_ids`` map *normalized* names → game ids; unresolved
    names keep ``id: None`` so the frontend can fall back to text.
    """
    icons: list[dict] = []
    for entry in spec["items"]:
        icon = {"type": "item",
                "id": entry.get("item_id") or item_ids.get(_norm(entry["name"])),
                "name": entry["name"]}
        if entry.get("quantity"):
            icon["quantity"] = entry["quantity"]
        icons.append(icon)
    for name in spec["npcs"]:
        icons.append({"type": "npc", "id": npc_ids.get(_norm(name)), "name": name})
    for name in spec["skills"]:
        # Metrics icons are keyed by name, not numeric id (img/metrics/{name}.png).
        icons.append({"type": "skill", "id": None, "name": name})

    total = len(icons)
    return {
        "badge": spec["badge"],
        "value": spec["value"],
        "icons": icons[:MAX_TILE_ICONS],
        "icon_overflow": max(total - MAX_TILE_ICONS, 0),
    }
