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
import os

from db import ItemList, NpcList
from web_api.common import abort_problem
from web_api.routes.npc_source_aliases import expand_source_names

# Hosts a loot_sweep group image may come from — our own upload CDN + site.
# Arbitrary external URLs are rejected (admins can only point at images we host).
_ALLOWED_IMAGE_HOSTS = (
    os.getenv("B2_CDN_BASE_URL", "https://videos.droptracker.io").rstrip("/"),
    "https://www.droptracker.io",
    "https://droptracker.io",
)


def _validated_image_url(raw) -> str | None:
    """A safe image URL for a loot_sweep group (uploaded CDN URL or a relative
    path on our domain), or None. 422 on an external/garbage URL."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        abort_problem(422, "Invalid config", "Group image_url must be a string URL.")
    url = raw.strip()
    if not url:
        return None
    if len(url) > 255:
        abort_problem(422, "Invalid config", "Group image_url is too long.")
    if url.startswith("/") or any(url.startswith(h + "/") for h in _ALLOWED_IMAGE_HOSTS):
        return url
    abort_problem(422, "Invalid image",
                  "Group images must be uploaded through DropTracker (external URLs aren't allowed).")

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
# point_collection per-item weight ceiling (same bound as loot_sweep items).
MAX_ITEM_POINTS = 1_000_000
# Serialized config ceiling — safely under the web_event_tasks.config TEXT
# column's 65535-byte limit so an oversized item list / source-NPC map is
# rejected with a 422 rather than truncating or 500-ing on save.
MAX_CONFIG_BYTES = 60000
# kc_target may list several NPCs ("kill 50 of any Dagannoth King") via
# config.npcs — a kill of ANY listed NPC advances the one shared counter.
MAX_KC_NPCS = 10
# item_collection tasks may optionally restrict which NPC(s) an item must drop
# from: config.source_npcs (single-item) / config.item_npcs (per-item map). The
# picker seeds the restriction with EVERY known wiki drop source of the item
# (the configurator then prunes), so this cap must clear the item-page source
# limit (web_api/routes/items.py _SOURCES_LIMIT) or seeding a high-source item
# would 422 on save.
MAX_SOURCE_NPCS = 100

# Loot Sweep (loot_sweep kind) config bounds — v2 (nested groups). Kept in sync
# with services/loot_sweep.py, which can't be imported here (this module's
# pytest conftest stubs the whole ``services`` package, so a real import fails).
LOOT_SWEEP_DECAY_MODES = ("linear", "geometric")
LOOT_SWEEP_DEFAULT_DECAY_PERCENT = 20
LOOT_SWEEP_DEFAULT_AWARDS_PER_TIER = 1
LOOT_SWEEP_DEFAULT_TIERS = 5
# A set/section completion scores like a decaying item (full, then 80%, 60% …),
# so by default it pays over the decay-tier count of completions rather than
# once. Must match services.loot_sweep.DEFAULT_{GROUP,SET}_BONUS_MAX.
LOOT_SWEEP_DEFAULT_BONUS_MAX = LOOT_SWEEP_DEFAULT_TIERS
LOOT_SWEEP_MAX_GROUPS = 40
LOOT_SWEEP_MAX_ITEMS = 400          # across the whole task
LOOT_SWEEP_MAX_AWARDS_PER_TIER = 20
LOOT_SWEEP_MAX_TOTAL_AWARDS = 200
LOOT_SWEEP_MAX_MATCH_NAMES = 10   # extra "also counts as" names per entry
LOOT_SWEEP_MAX_REQUIRED = 100     # receipts a group can demand of one entry
LOOT_SWEEP_MAX_NPCS = 30
LOOT_SWEEP_MAX_ITEM_POINTS = 1_000_000
LOOT_SWEEP_MAX_BONUS_POINTS = 10_000_000
LOOT_SWEEP_MAX_BONUS_MAX = 100

# any_path progress is tracked as a percentage of the closest-to-done path
# (paths differ in size, so a raw item count would be meaningless).
ANY_PATH_THRESHOLD = 100

# Metric alternatives an any_path path may be instead of an item checklist
# ("boss pet OR 5,000 GWD kills"). Kept in sync with
# services.event_engine.PATH_METRICS, which can't be imported here (the unit
# conftest stubs the whole ``services`` package).
PATH_METRICS = ("kc", "loot_value")

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


def _validated_source_npcs(s, raw) -> list[str]:
    """Canonicalize a source-NPC restriction list (``config.source_npcs`` or one
    value of ``config.item_npcs``); 422 on unknown names. ``None`` -> ``[]``.

    Mirrors the ``loot_value`` / ``kc_target`` NPC validators — the field name
    ``source_npcs`` matches what ``services.event_engine`` already reads for
    loot_value."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        abort_problem(422, "Invalid config",
                      "A source-NPC restriction must be an array of NPC names.")
    if not raw:
        return []  # empty selection = unrestricted (the restriction is opt-in)
    if len(raw) > MAX_SOURCE_NPCS:
        abort_problem(422, "Invalid config", f"At most {MAX_SOURCE_NPCS} source NPCs.")
    out, unknown = [], []
    for n in raw:
        # A display alias ("Wintertodt") expands to the real recorded source
        # names, so stored configs only ever hold names drops actually carry.
        for real in expand_source_names(str(n)):
            canonical = _canonical_npc(s, real)
            if not canonical:
                unknown.append(str(real).strip() or "(empty)")
            elif canonical not in out:
                out.append(canonical)
    if unknown:
        abort_problem(
            422, "Unknown NPC(s)",
            "Not found in the NPC database (exact in-game names required): "
            + ", ".join(sorted(set(unknown))[:10]),
        )
    return out


def _config_item_name_set(config: dict) -> set[str]:
    """Lower-cased set of every item name a normalized item_collection config
    references (flat ``items``, ``groups[].items``, or ``paths[].groups[].items``).
    Used to keep an ``item_npcs`` map from naming items outside the task."""
    names: set[str] = set()

    def _name_of(it):
        if isinstance(it, str):
            return it
        if isinstance(it, dict):
            return it.get("item_name") or it.get("name")
        return None

    for it in (config.get("items") or []):
        n = _name_of(it)
        if n:
            names.add(str(n).lower())
    groups = list(config.get("groups") or [])
    for path in (config.get("paths") or []):
        if isinstance(path, dict):
            groups.extend(path.get("groups") or [])
    for group in groups:
        if isinstance(group, dict):
            for it in (group.get("items") or []):
                n = _name_of(it)
                if n:
                    names.add(str(n).lower())
    return names


def _validated_item_npcs(s, raw, allowed_items: set[str]) -> dict:
    """Validate a ``config.item_npcs`` map (``{item_name: [npc, ...]}``) used by
    multi-item item_collection tasks to restrict individual items to specific
    drop sources. Each key must canonicalize to a known item, each value is a
    validated source-NPC list. Entries whose item isn't in the task's list are
    dropped (keeps the form forgiving); unknown item names / NPC names 422.
    Returns a canonical ``{item_name: [npc, ...]}`` with only non-empty entries."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        abort_problem(422, "Invalid config",
                      "'item_npcs' must be an object mapping item name to a list of NPC names.")
    if len(raw) > MAX_CONFIG_ITEMS:
        abort_problem(422, "Invalid config", f"At most {MAX_CONFIG_ITEMS} source-restricted items.")
    out: dict = {}
    unknown_items: list[str] = []
    for iname, npcs in raw.items():
        canonical = _canonical_item(s, str(iname))
        if not canonical:
            unknown_items.append(str(iname).strip() or "(empty)")
            continue
        if canonical.lower() not in allowed_items:
            continue
        validated = _validated_source_npcs(s, npcs)
        if validated:
            out[canonical] = validated
    if unknown_items:
        abort_problem(
            422, "Unknown item(s)",
            "A source restriction names an item not in the item database: "
            + ", ".join(sorted(set(unknown_items))[:10]),
        )
    return out


def _pet_name_request_set(raw) -> set[str]:
    """Lower-cased names the client flagged as pets (``config.pet_items``);
    422 on a malformed value. Names not actually present in the item list are
    simply ignored downstream (forgiving, like ``item_npcs``)."""
    if raw is None:
        return set()
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        abort_problem(422, "Invalid config", "'pet_items' must be an array of pet names.")
    if len(raw) > MAX_CONFIG_ITEMS:
        abort_problem(422, "Invalid config", f"At most {MAX_CONFIG_ITEMS} pet items.")
    return {n.strip().lower() for n in raw if n.strip()}


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


def _validated_item_entries(s, items, *, with_points: bool,
                            pet_names: set[str] | None = None,
                            found_pets: dict | None = None) -> list[dict]:
    """Canonicalize one item list. Names the client flagged as pets
    (``pet_names``, lower-cased) validate against the pet taxonomy instead of
    the item DB; each resolved pet lands in ``found_pets`` (lower -> canonical)
    so the caller can rebuild a canonical ``config.pet_items``."""
    if not isinstance(items, list) or not items:
        abort_problem(422, "Invalid config", "Item list config requires a non-empty 'items' array.")
    if len(items) > MAX_CONFIG_ITEMS:
        abort_problem(422, "Invalid config", f"At most {MAX_CONFIG_ITEMS} items per task.")
    from utils.osrs_pets import canonical_pet_name

    out, unknown, unknown_pets = [], [], []
    for it in items:
        if isinstance(it, str):
            name, entry = it, {}
        elif isinstance(it, dict):
            name = it.get("item_name") or it.get("name") or ""
            entry = it
        else:
            abort_problem(422, "Invalid config", "Each config item must be a name or object.")
        if pet_names and str(name).strip().lower() in pet_names:
            canonical = canonical_pet_name(name)
            if not canonical:
                unknown_pets.append(str(name).strip() or "(empty)")
                continue
            if found_pets is not None:
                found_pets[canonical.lower()] = canonical
        else:
            canonical = _canonical_item(s, name)
        if not canonical:
            unknown.append(str(name).strip() or "(empty)")
            continue
        row = {"item_name": canonical}
        if with_points:
            # Whole-number weights only (mirrors _validated_ls_item): the UI
            # used to allow decimals, so round anything historical/stale up
            # into an int ≥ 1.
            try:
                pts = int(round(float(entry.get("points") or 1)))
            except (TypeError, ValueError):
                pts = 1
            row["points"] = min(max(pts, 1), MAX_ITEM_POINTS)
        out.append(row)
    if unknown_pets:
        abort_problem(
            422, "Unknown pet(s)",
            "Not a known OSRS pet: " + ", ".join(sorted(set(unknown_pets))[:10]),
        )
    if unknown:
        abort_problem(
            422,
            "Unknown item(s)",
            "Not found in the item database (exact in-game names required): "
            + ", ".join(sorted(set(unknown))[:10]),
        )
    return out


def _validated_groups(s, groups, *, pet_names: set[str] | None = None,
                      found_pets: dict | None = None) -> tuple[list[dict], int]:
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
        entries = _validated_item_entries(s, group.get("items"), with_points=False,
                                          pet_names=pet_names, found_pets=found_pets)
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


def _validated_metric_path(s, path: dict, pi: int) -> dict:
    """Validate one METRIC path of an any_path config: a KC or loot-value
    alternative to an item checklist ("boss pet OR 5,000 GWD kills").

    ``kc`` needs at least one NPC (a kill of any listed NPC counts, kc_target
    semantics) and a kill-count ``need``; ``loot_value`` folds drop GP toward
    a GP ``need``, optionally restricted to source NPCs.
    """
    metric = path.get("metric")
    npcs_raw = path.get("npcs")
    if isinstance(npcs_raw, str):
        npcs_raw = [npcs_raw]
    if npcs_raw is not None and not isinstance(npcs_raw, list):
        abort_problem(422, "Invalid config", f"Path {pi}: 'npcs' must be a list of NPC names.")
    if metric == "kc":
        if not npcs_raw:
            abort_problem(422, "Invalid config",
                          f"Path {pi}: a kill-count path needs at least one NPC.")
        if len(npcs_raw) > MAX_KC_NPCS:
            abort_problem(422, "Invalid config",
                          f"Path {pi}: at most {MAX_KC_NPCS} NPCs per kill-count path.")
        need = _require_target_value(path.get("need"), what=f"Path {pi} kill count")
    elif metric == "loot_value":
        if npcs_raw is not None and len(npcs_raw) > MAX_SOURCE_NPCS:
            abort_problem(422, "Invalid config",
                          f"Path {pi}: at most {MAX_SOURCE_NPCS} source NPCs.")
        need = _require_target_value(path.get("need"), what=f"Path {pi} GP goal")
    else:
        abort_problem(422, "Invalid config",
                      f"Path {pi}: metric must be one of {list(PATH_METRICS)}.")
    canonical_npcs, unknown = [], []
    for name in (npcs_raw or []):
        # Source aliases ("Wintertodt") expand to the recorded drop-source
        # names, mirroring the kc_target / loot_value validators.
        for real in expand_source_names(str(name)):
            canonical = _canonical_npc(s, real)
            if not canonical:
                unknown.append(str(real).strip() or "(empty)")
            elif canonical not in canonical_npcs:
                canonical_npcs.append(canonical)
    if unknown:
        abort_problem(
            422, "Unknown NPC(s)",
            "Not found in the NPC database (exact in-game names required): "
            + ", ".join(sorted(set(unknown))[:10]),
        )
    norm: dict = {"metric": metric, "need": need}
    if canonical_npcs:
        norm["npcs"] = canonical_npcs
    label = path.get("label")
    if isinstance(label, str) and label.strip():
        norm["label"] = label.strip()[:80]
    return norm


def _validated_paths(s, paths, *, pet_names: set[str] | None = None,
                     found_pets: dict | None = None) -> tuple[list[dict], int]:
    """Validate a ``kind: "any_path"`` config: each path is its own
    groups-style requirement set — or a METRIC goal (``metric: "kc"`` /
    ``"loot_value"``) — and the task completes when ANY path is satisfied.
    "Dryness protection" tasks (suggestion #52): the full Justiciar set OR
    any 5 Justiciar items; or "Godwars boss pet OR 5,000 Godwars kills".

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
        if path.get("metric") is not None:
            out.append(_validated_metric_path(s, path, pi))
            continue
        groups, _need = _validated_groups(s, path.get("groups"),
                                          pet_names=pet_names, found_pets=found_pets)
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


def _validated_ls_item(s, raw, *, default_apt: int, unknown: list, seen: dict,
                       group_label: str) -> dict | None:
    """Validate one loot_sweep item; None if its name is unknown (collected in
    ``unknown``). ``seen`` maps item key -> group label to enforce that an item
    appears in only ONE group per task (so a receipt maps to one group)."""
    if isinstance(raw, str):
        name, entry = raw, {}
    elif isinstance(raw, dict):
        name, entry = (raw.get("item_name") or raw.get("name") or ""), raw
    else:
        abort_problem(422, "Invalid config", "Each Loot Sweep item must be a name or object.")
    source = entry.get("source") if entry.get("source") in ("drop", "pet") else "drop"
    # A "virtual" entry's name is a custom label ("Any ancestral piece"), not a
    # droppable item — its real items live entirely in match_names. Requested
    # explicitly, or inferred when a non-pet name doesn't resolve but aliases
    # are supplied.
    want_virtual = bool(entry.get("virtual"))
    virtual = False
    item_id = None
    if source == "pet":
        # A pet scores from a `pet` submission by name — validate against the
        # pet taxonomy, not the item DB; the item id (for the icon) is best
        # effort from the item DB.
        from utils.osrs_pets import canonical_pet_name
        canonical = canonical_pet_name(name)
        if not canonical:
            unknown.append(f"{str(name).strip() or '(empty)'} (pet)")
            return None
        _, item_id = _canonical_item_with_id(s, canonical)
    else:
        canonical, item_id = _canonical_item_with_id(s, name)
        if not canonical:
            # Allow it as a custom label ONLY if it will be backed by match_names
            # (checked after alias resolution below); otherwise it's a typo.
            label = str(name).strip()
            if (want_virtual or entry.get("match_names")) and label:
                canonical, item_id, virtual = label[:80], None, True
            else:
                unknown.append(label or "(empty)")
                return None
    key = canonical.lower()
    # A name may credit SEVERAL entries in the SAME group (additive: e.g. an
    # ancestral piece scores its own row AND feeds an "any 3 ancestral" pool),
    # but not across DIFFERENT groups — that would merge the two bosses' NPC
    # scopes and cross-credit their receipts.
    if key in seen and seen[key] != group_label:
        abort_problem(422, "Invalid config",
                      f"'{canonical}' is listed in more than one group "
                      f"('{seen[key]}' and '{group_label}') — an item belongs to one group.")
    seen.setdefault(key, group_label)
    try:
        pts = int(round(float(entry.get("points", 1) or 1)))
    except (TypeError, ValueError):
        abort_problem(422, "Invalid config", f"'{canonical}' points must be a number.")
    apt = _clamp_int(entry.get("awards_per_tier"), default=default_apt, lo=1,
                     hi=LOOT_SWEEP_MAX_AWARDS_PER_TIER)
    item: dict = {"item_name": canonical,
                  "points": min(max(pts, 1), LOOT_SWEEP_MAX_ITEM_POINTS)}
    if source == "pet":
        item["source"] = "pet"
    if item_id:
        item["item_id"] = int(item_id)
    if apt != LOOT_SWEEP_DEFAULT_AWARDS_PER_TIER:
        item["awards_per_tier"] = apt
    if entry.get("max_awards") is not None:
        item["max_awards"] = _clamp_int(
            entry.get("max_awards"), default=LOOT_SWEEP_DEFAULT_TIERS * apt,
            lo=1, hi=LOOT_SWEEP_MAX_TOTAL_AWARDS)
    # Membership defaults True; persist only the opt-out (pets / mega-rares that
    # score but must not gate their group's bonus).
    if entry.get("counts_for_group") is False or entry.get("counts_for_set") is False:
        item["counts_for_group"] = False
    # "Also counts as": alternate drop names crediting this same entry (the
    # vestige + gold-ring rule, or an "any ancestral piece" pool). Each alias
    # snaps to its canonical DB name and reserves it task-wide, so one receipt
    # can never credit two entries.
    raw_aliases = entry.get("match_names") or []
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    if not isinstance(raw_aliases, list):
        abort_problem(422, "Invalid config", f"'{canonical}': match_names must be a list.")
    if len(raw_aliases) > LOOT_SWEEP_MAX_MATCH_NAMES:
        abort_problem(422, "Invalid config",
                      f"'{canonical}': at most {LOOT_SWEEP_MAX_MATCH_NAMES} extra match names.")
    aliases: list[str] = []
    for a in raw_aliases:
        acanon, _aid = _canonical_item_with_id(s, a)
        if not acanon:
            unknown.append(f"{str(a).strip() or '(empty)'} (also-counts on {canonical})")
            continue
        akey = acanon.lower()
        if akey == key or any(x.lower() == akey for x in aliases):
            continue
        # Same-group reuse is allowed (additive scoring); cross-group is not.
        if akey in seen and seen[akey] != group_label:
            abort_problem(422, "Invalid config",
                          f"'{acanon}' already appears under a different group "
                          f"('{seen[akey]}') — a name can't credit entries in two groups.")
        seen.setdefault(akey, group_label)
        aliases.append(acanon)
    if aliases:
        item["match_names"] = aliases
    # A virtual (custom-label) entry is nothing without its real items.
    if virtual and not aliases:
        abort_problem(422, "Invalid config",
                      f"'{canonical}' isn't an item — add the real items it stands for "
                      f"under 'also counts as'.")
    if virtual:
        item["virtual"] = True
    # Receipts (any mix of this entry's names) the group demands before the
    # entry counts as collected — "any 3 ancestral pieces". Default 1.
    required = _clamp_int(entry.get("required"), default=1, lo=1, hi=LOOT_SWEEP_MAX_REQUIRED)
    if required != 1:
        item["required"] = required
    return item


def _validated_loot_sweep(s, config: dict | None) -> dict:
    """Validate + normalize a ``loot_sweep`` v2 config (nested groups).

    One task = one "set" of one or more **groups** (sub-sets). Each group ties
    items to source NPC(s) and has its own completion bonus; the task has a
    whole-set bonus for completing every group. Items are snapped to canonical
    DB names + ids and NPCs to canonical NpcList names.

    Result shape (what the engine + services/loot_sweep.py read)::

        {"kind":"loot_sweep", "decay_percent", "decay_mode",
         "set_bonus_points", "set_bonus_max",
         "groups": [{"label"?, "npcs":[str], "bonus_points", "bonus_max",
                     "items":[{"item_name","item_id"?,"points",
                               "awards_per_tier"?,"max_awards"?,
                               "counts_for_group"?}]}]}
    """
    if not isinstance(config, dict):
        abort_problem(422, "Invalid config",
                      "A Loot Sweep task needs a config object with a 'groups' array.")
    # v1 back-compat: a flat "items" list becomes one group carrying the set bonus.
    groups_raw = config.get("groups")
    if not groups_raw and isinstance(config.get("items"), list):
        groups_raw = [{"npcs": config.get("npcs") or [],
                       "bonus_points": config.get("set_bonus_points") or 0,
                       "items": config.get("items")}]
    if not isinstance(groups_raw, list) or not groups_raw:
        abort_problem(422, "Invalid config",
                      "A Loot Sweep task requires a non-empty 'groups' array.")
    if len(groups_raw) > LOOT_SWEEP_MAX_GROUPS:
        abort_problem(422, "Invalid config", f"At most {LOOT_SWEEP_MAX_GROUPS} groups per task.")

    decay_mode = config.get("decay_mode") or "linear"
    if decay_mode not in LOOT_SWEEP_DECAY_MODES:
        abort_problem(422, "Invalid config",
                      f"decay_mode must be one of {list(LOOT_SWEEP_DECAY_MODES)}.")
    decay_percent = _clamp_int(config.get("decay_percent"),
                               default=LOOT_SWEEP_DEFAULT_DECAY_PERCENT, lo=0, hi=100)
    set_bonus_points = _clamp_int(config.get("set_bonus_points"), default=0, lo=0,
                                  hi=LOOT_SWEEP_MAX_BONUS_POINTS)
    set_bonus_max = _clamp_int(config.get("set_bonus_max"),
                               default=LOOT_SWEEP_DEFAULT_BONUS_MAX, lo=1,
                               hi=LOOT_SWEEP_MAX_BONUS_MAX)

    out_groups, unknown_items, unknown_npcs, seen_items = [], [], [], {}
    total_items = 0
    for gi, graw in enumerate(groups_raw, start=1):
        if not isinstance(graw, dict):
            abort_problem(422, "Invalid config", f"Group {gi} must be an object.")
        label = str(graw.get("label") or "").strip()[:80]
        gl = label or f"group {gi}"
        npcs_raw = graw.get("npcs") or graw.get("npc") or []
        if isinstance(npcs_raw, str):
            npcs_raw = [npcs_raw]
        if not isinstance(npcs_raw, list):
            abort_problem(422, "Invalid config", f"Group {gl}: 'npcs' must be a list.")
        if len(npcs_raw) > LOOT_SWEEP_MAX_NPCS:
            abort_problem(422, "Invalid config", f"Group {gl}: at most {LOOT_SWEEP_MAX_NPCS} NPCs.")
        npcs = []
        for n in npcs_raw:
            canon = _canonical_npc(s, str(n))
            if not canon:
                unknown_npcs.append(str(n).strip() or "(empty)")
            elif canon not in npcs:
                npcs.append(canon)

        items_raw = graw.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            abort_problem(422, "Invalid config", f"Group {gl} requires a non-empty 'items' array.")
        default_apt = _clamp_int(graw.get("default_awards_per_tier"),
                                 default=LOOT_SWEEP_DEFAULT_AWARDS_PER_TIER, lo=1,
                                 hi=LOOT_SWEEP_MAX_AWARDS_PER_TIER)
        items = []
        for it in items_raw:
            v = _validated_ls_item(s, it, default_apt=default_apt, unknown=unknown_items,
                                   seen=seen_items, group_label=gl)
            if v is not None:
                items.append(v)
        total_items += len(items_raw)
        if total_items > LOOT_SWEEP_MAX_ITEMS:
            abort_problem(422, "Invalid config", f"At most {LOOT_SWEEP_MAX_ITEMS} items per task.")
        group: dict = {
            "npcs": npcs,
            "bonus_points": _clamp_int(graw.get("bonus_points"), default=0, lo=0,
                                       hi=LOOT_SWEEP_MAX_BONUS_POINTS),
            "bonus_max": _clamp_int(graw.get("bonus_max"),
                                    default=LOOT_SWEEP_DEFAULT_BONUS_MAX, lo=1,
                                    hi=LOOT_SWEEP_MAX_BONUS_MAX),
            "items": items,
        }
        if label:
            group["label"] = label
        img = _validated_image_url(graw.get("image_url"))
        if img:
            group["image_url"] = img
        out_groups.append(group)

    if unknown_items:
        abort_problem(422, "Unknown item(s)",
                      "Not found in the item database (exact in-game names required): "
                      + ", ".join(sorted(set(unknown_items))[:10]))
    if unknown_npcs:
        abort_problem(422, "Unknown NPC(s)",
                      "Not found in the NPC database (exact in-game names required): "
                      + ", ".join(sorted(set(unknown_npcs))[:10]))

    return {
        "kind": "loot_sweep",
        "decay_percent": decay_percent,
        "decay_mode": decay_mode,
        "set_bonus_points": set_bonus_points,
        "set_bonus_max": set_bonus_max,
        "groups": out_groups,
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
        # Optional source-NPC restriction and pet flags ride in the SAME config
        # object; captured up front because each branch rebuilds it from scratch.
        raw_source_npcs = (config or {}).get("source_npcs")
        raw_item_npcs = (config or {}).get("item_npcs")
        # Names in the list that are PETS (credited from pet submissions, not
        # drops/clogs). Validated against the pet taxonomy during entry
        # canonicalization; found_pets collects the canonical spellings.
        pet_names = _pet_name_request_set((config or {}).get("pet_items"))
        found_pets: dict = {}
        if kind is not None and kind not in ITEM_CONFIG_KINDS:
            abort_problem(
                422, "Invalid config",
                f"Item collection config kind must be one of {list(ITEM_CONFIG_KINDS)}.",
            )
        if config is not None and kind == "any_path":
            paths, tv = _validated_paths(s, config.get("paths"),
                                         pet_names=pet_names, found_pets=found_pets)
            config = {"kind": "any_path", "paths": paths}
            target = ""
        elif config is not None and kind == "groups":
            groups, tv = _validated_groups(s, config.get("groups"),
                                           pet_names=pet_names, found_pets=found_pets)
            config = {"kind": "groups", "groups": groups}
            target = ""
        elif config is not None and kind is not None:
            config = {
                "kind": kind,
                "items": _validated_item_entries(
                    s, config.get("items"), with_points=(kind == "point_collection"),
                    pet_names=pet_names, found_pets=found_pets,
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
            # Single item — plain ``target``, no ``kind``. A kind-less config
            # carrying only source_npcs (the single-item source restriction)
            # also lands here — but a config that has multi-item structure yet
            # forgot its ``kind`` is a malformed list task, not a silent narrow.
            if config is not None and (
                config.get("items") or config.get("groups") or config.get("paths")
            ):
                abort_problem(
                    422, "Invalid config",
                    f"Item collection config kind must be one of {list(ITEM_CONFIG_KINDS)}.",
                )
            canonical = _canonical_item(s, target)
            if not canonical:
                abort_problem(
                    422, "Unknown item",
                    f"'{target or '(empty)'}' is not in the item database — the exact "
                    "in-game item name is required.",
                )
            target = canonical
            tv = _require_target_value(tv if tv is not None else 1, what="Quantity")
            sources = _validated_source_npcs(s, raw_source_npcs)
            config = {"source_npcs": sources} if sources else None
        # Per-item source restriction (multi-item kinds): validate the item_npcs
        # map against the normalized item list and attach it. Single-item tasks
        # (no kind) use source_npcs above instead. Pets are excluded — they have
        # no drop source, and their canonical names land in config.pet_items.
        if config is not None and config.get("kind"):
            if found_pets:
                config["pet_items"] = sorted(found_pets.values())
            allowed_items = _config_item_name_set(config) - set(found_pets.keys())
            item_npcs = _validated_item_npcs(s, raw_item_npcs, allowed_items)
            if item_npcs:
                config["item_npcs"] = item_npcs

    elif ttype in ("kc_target", "pb_target"):
        # kc_target: optionally several NPCs (config.npcs) — a kill of ANY of
        # them counts toward the one kill-count goal. pb_target stays single.
        npcs_raw = (config or {}).get("npcs") if ttype == "kc_target" else None
        if npcs_raw is not None:
            if not isinstance(npcs_raw, list) or not npcs_raw:
                abort_problem(422, "Invalid config",
                              "'npcs' must be a non-empty array of NPC names.")
            if len(npcs_raw) > MAX_KC_NPCS:
                abort_problem(422, "Invalid config",
                              f"At most {MAX_KC_NPCS} NPCs per kill-count task.")
        names = [str(n) for n in npcs_raw] if npcs_raw is not None else [target]
        canonical_npcs, unknown = [], []
        for name in names:
            # kc_target lists support "a kill of ANY counts", so a source alias
            # ("Wintertodt") expands to its member NPCs. pb_target stays exact.
            for real in expand_source_names(name) if ttype == "kc_target" else [name]:
                canonical = _canonical_npc(s, real)
                if not canonical:
                    unknown.append(str(real).strip() or "(empty)")
                elif canonical not in canonical_npcs:
                    canonical_npcs.append(canonical)
        if unknown:
            abort_problem(
                422, "Unknown NPC(s)",
                "Not found in the NPC database (exact in-game names required): "
                + ", ".join(sorted(set(unknown))[:10]),
            )
        target = canonical_npcs[0]
        what = "Kill count" if ttype == "kc_target" else "Target time (seconds)"
        tv = _require_target_value(tv, what=what)
        # A single-NPC list collapses to plain target semantics (config-free),
        # so the engine's legacy per-task KC state keys stay stable.
        config = {"npcs": canonical_npcs} if len(canonical_npcs) > 1 else None

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
            for real in expand_source_names(str(name)):
                canonical = _canonical_npc(s, real)
                if not canonical:
                    abort_problem(
                        422, "Unknown NPC",
                        f"'{str(real).strip() or '(empty)'}' is not in the NPC database — the "
                        "exact in-game NPC name is required.",
                    )
                if canonical not in sources:
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
    serialized = json.dumps(config) if config else None
    # web_event_tasks.config is a MySQL TEXT column (~64KB). A pathological
    # config (e.g. a 100-item task each restricted to ~100 source NPCs, or a
    # huge loot_sweep) can overflow it and 500 on save — reject early with a
    # clear message instead.
    if serialized is not None and len(serialized) > MAX_CONFIG_BYTES:
        abort_problem(
            422, "Configuration too large",
            "This task's configuration is too large to store — reduce the number of "
            "items or restricted source NPCs.",
        )
    return {
        "target": target[:120] or None,
        "target_value": tv,
        "config": serialized,
    }
