"""``/v2/collection-log`` — the game's collection log structure, as reference data.

Not about any player. This is the catalogue an integration needs to make
sense of ``clog_slots``: which item ids are collection log slots, what each is
called, and how they group into tabs and pages — read from the OSRS game
cache by ``scripts/sync_collection_log.py`` and refreshed weekly after the
game update (``droptracker-clog-structure.timer``). Consumers that keep a
local copy of "which ids count" (the clan point bots do) should fetch this
rather than hand-maintain it: a new boss lands, the timer runs, and the
next fetch already knows the drop.

Served from the same manifest row the RuneLite plugin renders the in-game
page from, so the API and the client can never disagree about what a slot is.
"""
from __future__ import annotations

import json

from quart import Blueprint

from data_api.serving import serve_fixed

reference_bp = Blueprint("reference", __name__)

#: The ``plugin_manifest_sections`` row written by scripts/sync_collection_log.py.
MANIFEST_KEY = "collection_log"

#: One 50 KB row and ~1,700 names to serialise. Measured at well under a
#: millisecond of database time; the price is for the serialisation and the
#: bandwidth, and to stop a client polling it in a loop when it changes weekly.
COLLECTION_LOG_COST = 20


def _unix(value):
    import calendar

    if value is None or not hasattr(value, "timetuple"):
        return None
    return int(calendar.timegm(value.timetuple()))


def collection_log_payload(row_payload: str, updated_at) -> dict | None:
    """Shape the stored manifest section for API consumers.

    ``tabs`` is the manifest as stored (name → pages → parallel ``items`` /
    ``names`` arrays). ``items`` flattens that into ``{id: name}`` for the
    consumer that only wants to ask "is this id a slot, and what is it
    called?" — the same id appearing on two pages (a few do) resolves to the
    first name seen, which is the game's own order.
    """
    try:
        tabs = json.loads(row_payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(tabs, list):
        return None

    items: dict = {}
    slot_count = 0
    for tab in tabs:
        for page in tab.get("pages", []):
            ids = page.get("items") or []
            names = page.get("names") or []
            slot_count += len(ids)
            for index, item_id in enumerate(ids):
                name = names[index] if index < len(names) else ""
                items.setdefault(str(int(item_id)), name or None)

    return {
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else None,
        "updated_at_unix": _unix(updated_at),
        "source": "osrs game cache (scripts/sync_collection_log.py)",
        "slot_count": slot_count,
        "distinct_items": len(items),
        "tabs": tabs,
        "items": items,
    }


@reference_bp.route("/collection-log", methods=["GET"])
async def collection_log():
    """Every collection log slot: id, name, and its tab/page.

    Any valid key may read it — it is game data, not player data, and there
    is nothing to scope. Flat cost; cacheable for an hour, and it only changes
    when the weekly refresh finds a game update.
    """
    def build(session):
        from sqlalchemy import text

        row = session.execute(text(
            "SELECT payload, updated_at FROM plugin_manifest_sections WHERE `key` = :k"
        ).bindparams(k=MANIFEST_KEY)).first()
        if row is None:
            return None
        return collection_log_payload(row[0], row[1])

    return await serve_fixed(
        "reference.collection_log", COLLECTION_LOG_COST, build,
        not_found_detail="No collection log structure has been published yet.")
