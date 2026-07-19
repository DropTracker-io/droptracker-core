"""Per-type validation for event task writes (create / bulk create / patch).

Mirrors the constraints ``services/event_engine.py`` actually evaluates so a
task that saves is a task that can complete: unknown item/NPC names or missing
numeric goals are rejected with a 422 instead of silently creating a task the
engine will never match (the failure mode of the first-pass generic form).

Shared by ``routes/events.py`` (single create), ``routes/event_admin.py``
(bingo-designer bulk create + per-task PATCH). Pure DB lookups only — no
service imports (pytest conftest stubs ``services``).
"""
from __future__ import annotations

import json

from db import ItemList, NpcList
from web_api.common import abort_problem

# Canonical OSRS skill names as RuneLite reports them in experience payloads.
OSRS_SKILLS = (
    "Attack", "Strength", "Defence", "Ranged", "Prayer", "Magic",
    "Runecraft", "Hitpoints", "Crafting", "Mining", "Smithing", "Fishing",
    "Cooking", "Firemaking", "Woodcutting", "Agility", "Herblore",
    "Thieving", "Fletching", "Slayer", "Farming", "Construction", "Hunter",
)
_SKILL_BY_NORM = {s.lower(): s for s in OSRS_SKILLS}

# Item-collection config kinds the engine understands (any_of/all_of via the
# generic items map, point_collection weighting, assembly best-effort,
# groups combining all-of/any-of sub-requirements, any_path completing on
# whichever alternative requirement set finishes first).
ITEM_CONFIG_KINDS = ("any_of", "all_of", "point_collection", "assembly", "groups",
                     "any_path")

MAX_CONFIG_ITEMS = 100
MAX_CONFIG_GROUPS = 10
MAX_CONFIG_PATHS = 4

# Loot Sweep (loot_sweep kind) config bounds. Kept in sync with
# services/loot_sweep.py — which can't be imported here (this module's pytest
# conftest stubs the whole ``services`` package, so a real import would fail).
LOOT_SWEEP_DECAY_MODES = ("linear", "geometric")
LOOT_SWEEP_DEFAULT_DECAY_PERCENT = 20
LOOT_SWEEP_DEFAULT_MAX_AWARDS = 5
LOOT_SWEEP_DEFAULT_SET_BONUS_MAX = 1
LOOT_SWEEP_MAX_AWARDS_CAP = 100
LOOT_SWEEP_MAX_ITEM_POINTS = 1_000_000
LOOT_SWEEP_MAX_SET_BONUS_POINTS = 10_000_000
LOOT_SWEEP_MAX_SET_BONUS_MAX = 100

# any_path progress is tracked as a percentage of the closest-to-done path
# (paths differ in size, so a raw item count would be meaningless).
ANY_PATH_THRESHOLD = 100

# Non-semantic config keys preserved verbatim across validation (the bingo
# designer's auto-created marker — see event_admin._BINGO_AUTO_KEY).
_PASSTHROUGH_KEYS = ("bingo_auto",)


def _canonical_item(s, name: str) -> str | None:
    row = (
        s.query(ItemList.item_name)
        .filter(ItemList.item_name == (name or "").strip())
        .first()
    )
    return row[0] if row else None


def _canonical_item_with_id(s, name: str) -> tuple[str | None, int | None]:
    """Canonical item name + game id (for config icon refs), or (None, None)."""
    row = (
        s.query(ItemList.item_name, ItemList.item_id)
        .filter(ItemList.item_name == (name or "").strip())
        .first()
    )
    return (row[0], row[1]) if row else (None, None)


def _clamp_int(value, *, default: int, lo: int, hi: int) -> int:
    """Coerce to int and clamp to [lo, hi]; ``default`` when absent/garbage."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return default
    return min(max(value, lo), hi)


def _canonical_npc(s, name: str) -> str | None:
    row = (
        s.query(NpcList.npc_name)
        .filter(NpcList.npc_name == (name or "").strip())
        .first()
    )
    return row[0] if row else None


def _require_target_value(tv, *, what: str, lo: int = 1, hi: int | None = None) -> int:
    if not isinstance(tv, int) or isinstance(tv, bool) or tv < lo or (hi is not None and tv > hi):
        bounds = f"an integer ≥ {lo}" if hi is None else f"an integer between {lo} and {hi}"
        abort_problem(422, "Invalid target value", f"{what} must be {bounds}.")
    return tv


def _parse_config(raw) -> dict | None:
    """Accept a dict or JSON string; None when absent; 422 on garbage."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            abort_problem(422, "Invalid config", "Task config must be valid JSON.")
        if not isinstance(parsed, dict):
            abort_problem(422, "Invalid config", "Task config must be a JSON object.")
        return parsed
    abort_problem(422, "Invalid config", "Task config must be a JSON object or string.")


def _validated_item_entries(s, items, *, with_points: bool) -> list[dict]:
    if not isinstance(items, list) or not items:
        abort_problem(422, "Invalid config", "Item list config requires a non-empty 'items' array.")
    if len(items) > MAX_CONFIG_ITEMS:
        abort_problem(422, "Invalid config", f"At most {MAX_CONFIG_ITEMS} items per task.")
    out, unknown = [], []
    for it in items:
        if isinstance(it, str):
            name, entry = it, {}
        elif isinstance(it, dict):
            name = it.get("item_name") or it.get("name") or ""
            entry = it
        else:
            abort_problem(422, "Invalid config", "Each config item must be a name or object.")
        canonical = _canonical_item(s, name)
        if not canonical:
            unknown.append(str(name).strip() or "(empty)")
            continue
        row = {"item_name": canonical}
        if with_points:
            try:
                pts = float(entry.get("points") or 1)
            except (TypeError, ValueError):
                pts = 1.0
            row["points"] = max(pts, 0.1)
        out.append(row)
    if unknown:
        abort_problem(
            422,
            "Unknown item(s)",
            "Not found in the item database (exact in-game names required): "
            + ", ".join(sorted(set(unknown))[:10]),
        )
    return out


def _validated_groups(s, groups) -> tuple[list[dict], int]:
    """Validate a ``kind: "groups"`` config: every group is its own all-of or
    any-of item requirement, and the task completes when all groups are
    satisfied (e.g. a godsword: ALL three shards + ANY one hilt).

    Returns ``(normalized groups, total target_value)`` where each group is
    ``{"mode", "items", "need"}`` — need is len(items) for all_of, the
    requested count (default 1) for any_of.
    """
    if not isinstance(groups, list) or not groups:
        abort_problem(422, "Invalid config", "Grouped config requires a non-empty 'groups' array.")
    if len(groups) > MAX_CONFIG_GROUPS:
        abort_problem(422, "Invalid config", f"At most {MAX_CONFIG_GROUPS} requirement groups per task.")
    out: list[dict] = []
    seen: dict[str, int] = {}
    total_items = 0
    total_need = 0
    for gi, group in enumerate(groups, start=1):
        if not isinstance(group, dict):
            abort_problem(422, "Invalid config", "Each requirement group must be an object.")
        mode = group.get("mode")
        if mode not in ("all_of", "any_of"):
            abort_problem(422, "Invalid config",
                          f"Group {gi}: mode must be 'all_of' or 'any_of'.")
        entries = _validated_item_entries(s, group.get("items"), with_points=False)
        names = [e["item_name"] for e in entries]
        for name in names:
            key = name.lower()
            if key in seen:
                abort_problem(
                    422, "Invalid config",
                    f"'{name}' appears in more than one requirement group — an item "
                    "can only count toward one group.",
                )
            seen[key] = gi
        total_items += len(names)
        if total_items > MAX_CONFIG_ITEMS:
            abort_problem(422, "Invalid config", f"At most {MAX_CONFIG_ITEMS} items per task.")
        if mode == "any_of":
            # Quantities fold, so 'need' may legitimately exceed the list
            # length (e.g. any 500 javelins across the javelin types).
            need = _require_target_value(group.get("need", 1), what=f"Group {gi} 'need'")
            out.append({"mode": "any_of", "items": names, "need": need})
            total_need += need
        else:
            out.append({"mode": "all_of", "items": names, "need": len(names)})
            total_need += len(names)
    return out, total_need


def _validated_paths(s, paths) -> tuple[list[dict], int]:
    """Validate a ``kind: "any_path"`` config: each path is its own
    groups-style requirement set and the task completes when ANY path is
    satisfied — "dryness protection" tasks (suggestion #52), e.g. the full
    Justiciar set OR any 5 Justiciar items.

    Items may repeat across paths (the same drop advances every path that
    lists it) but not within one path (enforced per-path by
    :func:`_validated_groups`). Returns ``(normalized paths, target_value)``
    with the threshold pinned to :data:`ANY_PATH_THRESHOLD` because progress
    is a percentage of the closest path, not an item count.
    """
    if not isinstance(paths, list) or len(paths) < 2:
        abort_problem(422, "Invalid config",
                      "Either-or config requires at least two paths.")
    if len(paths) > MAX_CONFIG_PATHS:
        abort_problem(422, "Invalid config",
                      f"At most {MAX_CONFIG_PATHS} paths per task.")
    out: list[dict] = []
    total_items = 0
    for pi, path in enumerate(paths, start=1):
        if not isinstance(path, dict):
            abort_problem(422, "Invalid config", f"Path {pi} must be an object.")
        groups, _need = _validated_groups(s, path.get("groups"))
        total_items += sum(len(g["items"]) for g in groups)
        if total_items > MAX_CONFIG_ITEMS:
            abort_problem(422, "Invalid config",
                          f"At most {MAX_CONFIG_ITEMS} items per task.")
        norm: dict = {"groups": groups}
        label = path.get("label")
        if isinstance(label, str) and label.strip():
            norm["label"] = label.strip()[:80]
        out.append(norm)
    return out, ANY_PATH_THRESHOLD


def _validated_loot_sweep(s, config: dict | None) -> dict:
    """Validate + normalize a ``loot_sweep`` task config (one task = one boss
    "set"). Items are checked against the item DB and snapped to canonical
    names + game ids; the set/decay parameters are clamped to sane bounds.

    Result shape (what the engine + services/loot_sweep.py read)::

        {"kind": "loot_sweep", "decay_percent", "decay_mode",
         "default_max_awards", "set_bonus_points", "set_bonus_max",
         "items": [{"item_name", "item_id"?, "points", "max_awards"?,
                    "counts_for_set"?}]}
    """
    if not isinstance(config, dict):
        abort_problem(422, "Invalid config",
                      "A Loot Sweep task needs a config object with an 'items' array.")
    items_raw = config.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        abort_problem(422, "Invalid config",
                      "A Loot Sweep task requires a non-empty 'items' array.")
    if len(items_raw) > MAX_CONFIG_ITEMS:
        abort_problem(422, "Invalid config", f"At most {MAX_CONFIG_ITEMS} items per task.")

    decay_mode = config.get("decay_mode")
    decay_mode = "linear" if decay_mode is None else decay_mode
    if decay_mode not in LOOT_SWEEP_DECAY_MODES:
        abort_problem(422, "Invalid config",
                      f"decay_mode must be one of {list(LOOT_SWEEP_DECAY_MODES)}.")
    decay_percent = _clamp_int(config.get("decay_percent"),
                               default=LOOT_SWEEP_DEFAULT_DECAY_PERCENT, lo=0, hi=100)
    default_max = _clamp_int(config.get("default_max_awards"),
                             default=LOOT_SWEEP_DEFAULT_MAX_AWARDS, lo=1,
                             hi=LOOT_SWEEP_MAX_AWARDS_CAP)
    set_bonus_points = _clamp_int(config.get("set_bonus_points"), default=0, lo=0,
                                  hi=LOOT_SWEEP_MAX_SET_BONUS_POINTS)
    set_bonus_max = _clamp_int(config.get("set_bonus_max"),
                               default=LOOT_SWEEP_DEFAULT_SET_BONUS_MAX, lo=1,
                               hi=LOOT_SWEEP_MAX_SET_BONUS_MAX)

    out_items, unknown, seen = [], [], set()
    for raw in items_raw:
        if isinstance(raw, str):
            name, entry = raw, {}
        elif isinstance(raw, dict):
            name = raw.get("item_name") or raw.get("name") or ""
            entry = raw
        else:
            abort_problem(422, "Invalid config",
                          "Each Loot Sweep item must be a name or an object.")
        canonical, item_id = _canonical_item_with_id(s, name)
        if not canonical:
            unknown.append(str(name).strip() or "(empty)")
            continue
        key = canonical.lower()
        if key in seen:
            continue  # same item listed twice — first wins
        seen.add(key)
        try:
            pts = int(round(float(entry.get("points", 1) or 1)))
        except (TypeError, ValueError):
            abort_problem(422, "Invalid config",
                          f"'{canonical}' points must be a number.")
        item: dict = {"item_name": canonical,
                      "points": min(max(pts, 1), LOOT_SWEEP_MAX_ITEM_POINTS)}
        if item_id:
            item["item_id"] = int(item_id)
        if entry.get("max_awards") is not None:
            item["max_awards"] = _clamp_int(entry.get("max_awards"), default=default_max,
                                            lo=1, hi=LOOT_SWEEP_MAX_AWARDS_CAP)
        # Set membership defaults True; only persist the opt-out (e.g. the pet,
        # which scores but must not gate set completion).
        if entry.get("counts_for_set") is False:
            item["counts_for_set"] = False
        out_items.append(item)
    if unknown:
        abort_problem(
            422, "Unknown item(s)",
            "Not found in the item database (exact in-game names required): "
            + ", ".join(sorted(set(unknown))[:10]),
        )

    return {
        "kind": "loot_sweep",
        "decay_percent": decay_percent,
        "decay_mode": decay_mode,
        "default_max_awards": default_max,
        "set_bonus_points": set_bonus_points,
        "set_bonus_max": set_bonus_max,
        "items": out_items,
    }


def validate_task_payload(s, body: dict) -> dict:
    """Validate + normalize a full task create payload.

    Returns normalized kwargs for ``EventTask`` (target/target_value/config as
    the engine expects them); raises 422 problems otherwise.
    """
    ttype = body.get("type")
    target = (body.get("target") or "").strip()
    tv = body.get("target_value")
    config = _parse_config(body.get("config"))
    passthrough = {k: config.pop(k) for k in _PASSTHROUGH_KEYS if config and k in config}
    if config == {}:
        config = None

    if ttype == "item_collection":
        kind = (config or {}).get("kind")
        if config is not None and kind not in ITEM_CONFIG_KINDS:
            abort_problem(
                422, "Invalid config",
                f"Item collection config kind must be one of {list(ITEM_CONFIG_KINDS)}.",
            )
        if config is not None and kind == "any_path":
            paths, tv = _validated_paths(s, config.get("paths"))
            config = {"kind": "any_path", "paths": paths}
            target = ""
        elif config is not None and kind == "groups":
            groups, tv = _validated_groups(s, config.get("groups"))
            config = {"kind": "groups", "groups": groups}
            target = ""
        elif config is not None:
            config = {
                "kind": kind,
                "items": _validated_item_entries(
                    s, config.get("items"), with_points=(kind == "point_collection")
                ),
            }
            target = ""
            if kind == "any_of":
                # "Any N from the list" — quantities fold, so a stack of a
                # listed item counts its size. Default: any single one.
                tv = _require_target_value(tv if tv is not None else 1, what="Quantity")
            elif kind in ("all_of", "assembly"):
                tv = len(config["items"])
            else:  # point_collection — points threshold to reach
                tv = _require_target_value(tv, what="Points goal")
        else:
            canonical = _canonical_item(s, target)
            if not canonical:
                abort_problem(
                    422, "Unknown item",
                    f"'{target or '(empty)'}' is not in the item database — the exact "
                    "in-game item name is required.",
                )
            target = canonical
            tv = _require_target_value(tv if tv is not None else 1, what="Quantity")

    elif ttype in ("kc_target", "pb_target"):
        canonical = _canonical_npc(s, target)
        if not canonical:
            abort_problem(
                422, "Unknown NPC",
                f"'{target or '(empty)'}' is not in the NPC database — the exact "
                "in-game NPC name is required.",
            )
        target = canonical
        what = "Kill count" if ttype == "kc_target" else "Target time (seconds)"
        tv = _require_target_value(tv, what=what)
        config = None

    elif ttype in ("xp_target", "skill_target"):
        skill = _SKILL_BY_NORM.get(target.lower())
        if not skill:
            abort_problem(422, "Unknown skill", f"'{target or '(empty)'}' is not an OSRS skill.")
        target = skill
        if ttype == "xp_target":
            tv = _require_target_value(tv, what="XP goal")
        else:
            tv = _require_target_value(tv, what="Target level", lo=2, hi=99)
        config = None

    elif ttype == "loot_value":
        tv = _require_target_value(tv, what="GP goal")
        sources = []
        for name in ((config or {}).get("source_npcs") or ([target] if target else [])):
            canonical = _canonical_npc(s, str(name))
            if not canonical:
                abort_problem(
                    422, "Unknown NPC",
                    f"'{str(name).strip() or '(empty)'}' is not in the NPC database — the "
                    "exact in-game NPC name is required.",
                )
            sources.append(canonical)
        target = ""
        config = {"source_npcs": sources} if sources else None

    elif ttype == "pet_collection":
        from utils.osrs_pets import canonical_pet_name, pet_categories
        if target:
            # Specific pet: must be a catalogued pet (misc included). The
            # matcher compares by name, so we snap it to the canonical spelling.
            canonical = canonical_pet_name(target)
            if not canonical:
                abort_problem(
                    422, "Unknown pet",
                    f"'{target or '(empty)'}' is not a known OSRS pet.",
                )
            target = canonical
            tv = _require_target_value(tv if tv is not None else 1, what="Quantity")
            config = None
        else:
            # Category / any-pet: validate the requested category keys, if any.
            categories = (config or {}).get("categories")
            if categories is not None:
                if not isinstance(categories, list) or not categories:
                    abort_problem(422, "Invalid config",
                                  "'categories' must be a non-empty array of pet categories.")
                known = set(pet_categories())
                bad = sorted({c for c in categories if c not in known})
                if bad:
                    abort_problem(
                        422, "Invalid config",
                        f"Unknown pet categor{'y' if len(bad) == 1 else 'ies'}: "
                        f"{', '.join(bad)}. Valid: {sorted(known)}.",
                    )
                config = {"categories": sorted(set(categories))}
            else:
                config = None  # bare "any pet" (misc excluded)
            target = ""
            tv = _require_target_value(tv if tv is not None else 1, what="Number of pets")

    elif ttype in ("ehp_target", "ehb_target"):
        tv = _require_target_value(tv, what="Target " + ttype[:3].upper())
        config = None

    elif ttype == "loot_sweep":
        # One task = one boss/"set". Scoring params + item list live in config;
        # target/target_value are unused (the task never "completes").
        config = _validated_loot_sweep(s, config)
        target = ""
        tv = None

    else:  # custom — free-form manual task
        if tv is not None and (not isinstance(tv, int) or isinstance(tv, bool) or tv < 0):
            abort_problem(422, "Invalid target value", "target_value must be a non-negative integer.")
        target = target[:120]

    config = {**(config or {}), **passthrough} or None
    return {
        "target": target[:120] or None,
        "target_value": tv,
        "config": json.dumps(config) if config else None,
    }
