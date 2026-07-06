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
# generic items map, point_collection weighting, assembly best-effort).
ITEM_CONFIG_KINDS = ("any_of", "all_of", "point_collection", "assembly")

MAX_CONFIG_ITEMS = 100

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
        if config is not None:
            config = {
                "kind": kind,
                "items": _validated_item_entries(
                    s, config.get("items"), with_points=(kind == "point_collection")
                ),
            }
            target = ""
            if kind == "any_of":
                tv = 1
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

    elif ttype in ("ehp_target", "ehb_target"):
        tv = _require_target_value(tv, what="Target " + ttype[:3].upper())
        config = None

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
