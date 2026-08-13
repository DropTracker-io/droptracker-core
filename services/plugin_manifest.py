"""Assembly of the plugin manifest served by ``GET /manifest``.

Kept separate from the route so the assembly and versioning rules can be tested
without standing up Quart, and so other callers (the regeneration script, the
admin surface) share exactly one definition of what a manifest is.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

# Deliberately stdlib-only: the ORM model is never imported here, so callers can
# pass anything with ``.key`` and ``.payload`` (rows, fakes, replayed JSON) and
# unit tests can exercise this module under the conftest's stubbed ``db``.

# Cache coordinates, defined here so the route (which reads it) and the
# regeneration script (which must invalidate it) cannot drift apart. A stale
# cache would mean an edit takes five minutes to reach anyone, which defeats the
# reason the manifest is server-side at all.
CACHE_KEY = "plugin:manifest"
CACHE_TTL_SECONDS = 300

# Seed contents for a database that has never had the manifest built. These are
# defaults, not the source of truth: once a row exists the database wins, so a
# fix can be applied by updating a row rather than shipping code.
#
# Every entry here should be something the plugin would otherwise hardcode.
DEFAULT_SECTIONS: Dict[str, Dict[str, Any]] = {
    "combat_achievement_varps": {
        "payload": [
            3116, 3117, 3118, 3119, 3120, 3121, 3122, 3123, 3124, 3125,
            3126, 3127, 3128, 3387, 3718, 3773, 3774, 4204, 4496, 4721, 5673,
        ],
        "description": (
            "Varps holding combat achievement completion bits (32 tasks each). "
            "Non-contiguous: Jagex appends a new varp at an arbitrary id when "
            "the previous one fills up."
        ),
    },
    "quest_ids": {
        "payload": [],
        "description": (
            "Quest ids to poll for state. Empty means the plugin falls back to "
            "RuneLite's Quest enum, which lags new quest releases."
        ),
    },
    "sync": {
        "payload": {
            # Server-tunable so sync load can be dialled back without a Plugin
            # Hub release.
            "interval_minutes": 60,
            "rapid_seconds": 3,
            # Kill switch. False makes conforming clients stop syncing state
            # entirely, which is the only lever we have that does not require a
            # plugin release, so it must be honoured before anything else.
            "enabled": True,
        },
        "description": "State-sync cadence and kill switch.",
    },
}


def manifest_version(sections: Dict[str, Any]) -> str:
    """Content hash of the section payloads.

    Derived rather than stored so no writer can forget to bump it: any change to
    any payload changes the version, and an unchanged manifest always reports
    the same version even if rows were rewritten with identical content.
    """
    canonical = json.dumps(sections, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def _decode(row) -> Any:
    """Section payload, or None when the stored JSON is unreadable.

    A corrupt row must not take the whole manifest down: every other section is
    still useful, and a plugin that receives a manifest missing one section
    falls back to its built-in behaviour for that section alone.
    """
    try:
        return json.loads(row.payload)
    except (TypeError, ValueError):
        return None


def assemble_sections(rows) -> Dict[str, Any]:
    """Section name -> payload, defaults filling anything absent or unreadable."""
    sections: Dict[str, Any] = {
        key: spec["payload"] for key, spec in DEFAULT_SECTIONS.items()
    }
    for row in rows:
        decoded = _decode(row)
        if decoded is None:
            continue
        sections[row.key] = decoded
    return sections


def manifest_payload(rows) -> Dict[str, Any]:
    """The full manifest document served to plugin clients."""
    sections = assemble_sections(rows)
    return {"version": manifest_version(sections), **sections}
