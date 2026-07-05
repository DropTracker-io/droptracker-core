"""Seed web_event_task_library from the legacy BoardGame task store.

Maps games/events/task_store/default.json (144 tasks: exact_item /
point_collection / assembly) onto the schema-v2 library (backend Task 15,
events-prd.md D2). Idempotent: upserts on (name, source='legacy_v1').

Legacy -> library mapping:
- exact_item, single item   -> type=item_collection, target=item name,
                               target_value=quantity
- exact_item, multiple items-> type=item_collection, config={kind:"any_of"?} —
                               legacy exact_item lists are "collect all", so
                               config={kind:"all_of", items:[...]}
- point_collection          -> type=item_collection,
                               config={kind:"point_collection", items:[{item_name, points}]},
                               target_value=<legacy points threshold>
- assembly                  -> type=item_collection,
                               config={kind:"assembly", items:[...]}

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.seed_event_task_library
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models.base import session as _default_session  # noqa: E402
from db.models import EventTaskLibraryItem  # noqa: E402

TASK_STORE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "games", "events", "task_store", "default.json",
)

SOURCE = "legacy_v1"


def _map_task(raw: dict) -> dict:
    name = (raw.get("name") or "").strip()[:120]
    description = (raw.get("description") or "").strip() or None
    difficulty = raw.get("difficulty") or None
    legacy_type = raw.get("type")
    items = raw.get("required_items") or []

    target = None
    target_value = None
    config = None

    if legacy_type == "exact_item" and len(items) == 1:
        target = (items[0].get("item_name") or "").strip()[:120] or None
        target_value = int(items[0].get("quantity") or 1)
    elif legacy_type == "exact_item":
        config = {"kind": "all_of", "items": items}
    elif legacy_type == "point_collection":
        config = {"kind": "point_collection", "items": items}
        target_value = int(raw.get("points") or 0) or None
    elif legacy_type == "assembly":
        config = {"kind": "assembly", "items": items}
    else:
        # Unknown legacy type: keep it, but only manually completable.
        config = {"kind": "legacy_unmapped", "legacy_type": legacy_type, "items": items}
        return {
            "name": name, "description": description, "type": "custom",
            "target": None, "target_value": None, "difficulty": difficulty,
            "config": json.dumps(config),
        }

    return {
        "name": name,
        "description": description,
        "type": "item_collection",
        "target": target,
        "target_value": target_value,
        "difficulty": difficulty,
        "config": json.dumps(config) if config is not None else None,
    }


def seed(session) -> tuple[int, int]:
    with open(TASK_STORE, "r") as f:
        tasks = json.load(f)["tasks"]

    # Keyed case-insensitively: MySQL's unique index uses a _ci collation and
    # the legacy JSON contains case-duplicate names.
    existing = {
        row.name.lower(): row
        for row in session.query(EventTaskLibraryItem).filter(EventTaskLibraryItem.source == SOURCE)
    }

    created = updated = 0
    seen: set[str] = set()
    for raw in tasks:
        mapped = _map_task(raw)
        if not mapped["name"] or mapped["name"].lower() in seen:
            continue
        seen.add(mapped["name"].lower())
        row = existing.get(mapped["name"].lower())
        if row is None:
            session.add(EventTaskLibraryItem(source=SOURCE, default_points=0, active=True, **mapped))
            created += 1
        else:
            changed = False
            for key, value in mapped.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
            updated += int(changed)
    session.commit()
    return created, updated


if __name__ == "__main__":
    created, updated = seed(_default_session)
    print(f"web_event_task_library seed: {created} created, {updated} updated (source={SOURCE})")
