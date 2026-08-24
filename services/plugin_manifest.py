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

# Sections that go over the wire to plugin clients.
#
# An allowlist, not "every row in the table", because the table holds two very
# different kinds of thing with two very different size budgets. The reference
# data the *server* reads — the combat achievement task registry and the
# collection log structure — is 156KB between them, and it is read straight from
# the database by the web API (see web_api/routes/player_state.py and
# api/routes/state_sync.py), never over HTTP. Serving it to clients as well made
# the manifest 160KB, of which the plugin used 239 bytes.
#
# Default-deny is deliberate, and it is not a hedge against size alone. A
# section the plugin has no field for cannot be used by it however we send it:
# consuming a new section needs new client code and therefore a Plugin Hub
# release either way. So shipping one unasked buys nothing and costs every
# client the bytes — and once cost them far more than bytes, when
# ``combat_achievement_tasks`` changed from an array to an object and every
# client lost the *entire* manifest, Gson having abandoned the document at the
# first field whose type did not match. The varps, the quest ids and the sync
# kill switch all went with it, and the only symptom was one debug line.
#
# Adding a key here is the deliberate act of putting it on the wire. Everything
# else stays server-side, editable at runtime, and free.
CLIENT_SECTIONS = (
    "combat_achievement_varps",
    "quest_ids",
    "sync",
)

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
    "combat_achievement_tasks": {
        # Server-only: not in CLIENT_SECTIONS, so it never reaches a plugin.
        # Populated by scripts/build_manifest.py from scripts/ca_tasks.json.
        # Empty here rather than inlined because it is 646 entries of generated
        # data, not a hand-maintained default.
        "payload": {},
        "description": (
            "Combat achievement tasks from the game cache: name, tier, monster, "
            "type, and the varp/bit that records completion. The varp/bit pair is "
            "what lets stored varps be decoded into named tasks."
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


def client_sections(rows) -> Dict[str, Any]:
    """The assembled sections, narrowed to what clients are served.

    Split from ``manifest_payload`` so the filtering rule can be asserted on
    directly, and so a caller that wants the whole picture (an admin surface,
    say) can still have it from ``assemble_sections``.
    """
    sections = assemble_sections(rows)
    return {key: sections[key] for key in CLIENT_SECTIONS if key in sections}


def manifest_payload(rows) -> Dict[str, Any]:
    """The manifest document served to plugin clients.

    The version hashes only the sections actually served. That is what makes it
    a usable cache validator: re-running the collection log sync changes a
    server-only section, and hashing that too would churn every client's ETag
    over data no client can see.
    """
    sections = client_sections(rows)
    return {"version": manifest_version(sections), **sections}
