"""OSRS map regions, by id and by name.

The server half of the plugin's ``RegionNameRegistry``. ``data/region_names.json``
is a byte-for-byte copy of the plugin resource, which was itself extracted
verbatim from RuneLite's ``DiscordGameEventType`` enum (BSD 2-Clause) — 397
named areas over 974 region ids, each filed under one of ``BOSSES``, ``RAIDS``,
``DUNGEONS``, ``CITIES``, ``MINIGAMES`` or ``REGIONS``.

Why the server needs its own copy rather than trusting the ``region_name`` the
plugin sends:

* the **region blacklist picker** has to list areas a leader has never died in,
  so it cannot be built from submitted payloads;
* a leader may blacklist "Castle Wars" while a submission arrives carrying only
  ``region_id`` (an older client, or a region the plugin could not name), and
  the two still have to match;
* the name is display text from an untrusted client — resolving the id here
  means a renamed or spoofed ``region_name`` cannot dodge a blacklist entry.

Regenerate by copying ``plugin/src/main/resources/io/droptracker/region_names.json``.
Do not hand-edit: divergence between the two copies is exactly the failure this
module exists to prevent.
"""
from __future__ import annotations

import json
import os
import threading

from utils.npc_names import npc_slug

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "region_names.json"
)

#: The coarse buckets RuneLite files areas under, for grouping the picker.
AREA_TYPES = ("BOSSES", "RAIDS", "DUNGEONS", "MINIGAMES", "CITIES", "REGIONS")

_lock = threading.Lock()
_loaded = False
#: region id -> {"name": str, "type": str}
_by_id: dict[int, dict[str, str]] = {}
#: normalized name -> set of region ids. A set, not an id: two distinct areas
#: share the name "Lighthouse", so a leader picking it must mute both.
_by_name: dict[str, set[int]] = {}
#: display name -> sorted region ids, for the picker.
_areas: list[dict] = []


def _load() -> None:
    """Parse the resource once, on first use.

    Failing to load is not fatal: every consumer degrades to id-only matching,
    which is what Dink offers anyway. A missing data file must not take the
    notification pipeline down with it.
    """
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        try:
            with open(_DATA_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return

        for area in payload.get("areas") or []:
            name = (area.get("name") or "").strip()
            regions = [int(r) for r in (area.get("regions") or [])]
            if not name or not regions:
                continue
            area_type = area.get("type") or "REGIONS"
            for region_id in regions:
                _by_id[region_id] = {"name": name, "type": area_type}
            _by_name.setdefault(region_key(name), set()).update(regions)
            _areas.append({"name": name, "type": area_type, "regions": sorted(regions)})

        _areas.sort(key=lambda a: (a["type"], a["name"]))


def region_key(name) -> str:
    """Normalized identity for an area name.

    Reuses ``npc_slug`` so "Castle Wars", "castle_wars" and "  Castle  Wars "
    are one key — the same normalization the item/NPC blacklist uses, so a
    leader's typing behaves identically across all three entry types.
    """
    return npc_slug(name)


def name_for(region_id) -> str | None:
    """The area name covering this region id, or ``None`` if unnamed.

    Two of the 97 safe region ids have no name (the clan hall and one
    player-owned-house chunk); callers show the bare id for those.
    """
    _load()
    try:
        entry = _by_id.get(int(region_id))
    except (TypeError, ValueError):
        return None
    return entry["name"] if entry else None


def type_for(region_id) -> str | None:
    """The coarse bucket this region is filed under, or ``None``."""
    _load()
    try:
        entry = _by_id.get(int(region_id))
    except (TypeError, ValueError):
        return None
    return entry["type"] if entry else None


def regions_for(name) -> set[int]:
    """Every region id belonging to the named area (empty when unknown)."""
    _load()
    key = region_key(name)
    return set(_by_name.get(key, ()))


def all_areas() -> list[dict]:
    """Every named area, sorted by type then name — the picker's source list."""
    _load()
    return [dict(area) for area in _areas]
