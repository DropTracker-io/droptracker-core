"""Seed curated pet_collection presets into web_event_task_library.

Gives admins off-the-shelf "obtain a pet" tasks in the create-task picker,
matching the engine's pet_collection semantics (utils/osrs_pets.py):

- target=None, config=None            -> any pet (misc excluded)
- target=None, config={categories:[]} -> any pet in those categories
- target="<Pet name>"                 -> that specific pet

Idempotent: upserts on (name, source='curated'). Category presets are derived
from utils.osrs_pets so a new category there is one edit away from a preset.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.seed_pet_tasks
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models.base import session as _default_session  # noqa: E402
from db.models import EventTaskLibraryItem  # noqa: E402
from utils.osrs_pets import DEFAULT_CATEGORIES  # noqa: E402

SOURCE = "curated"

# (name, description, target, config, default_points, difficulty)
_PRESETS: list[tuple] = [
    ("Obtain any pet",
     "Receive any pet drop (excludes trivial/stackable 'misc' pets).",
     None, None, 100, "fire"),
]

_CATEGORY_META = {
    "boss":     ("Obtain any boss pet", "Receive any boss pet.", 80, "fire"),
    "skilling": ("Obtain any skilling pet", "Receive any skilling pet.", 60, "earth"),
    "raids":    ("Obtain any raids pet", "Receive any Chambers/Theatre/Tombs pet.", 90, "fire"),
    "clue":     ("Obtain any clue pet", "Receive any Treasure Trails pet.", 90, "fire"),
    "minigame": ("Obtain any minigame pet", "Receive any minigame pet.", 80, "fire"),
}
for _cat in DEFAULT_CATEGORIES:
    meta = _CATEGORY_META.get(_cat)
    if not meta:
        continue
    name, desc, pts, diff = meta
    _PRESETS.append((name, desc, None, {"categories": [_cat]}, pts, diff))


def seed(session) -> tuple[int, int]:
    existing = {
        row.name.lower(): row
        for row in session.query(EventTaskLibraryItem).filter(
            EventTaskLibraryItem.source == SOURCE)
    }
    created = updated = 0
    for name, description, target, config, points, difficulty in _PRESETS:
        mapped = {
            "name": name,
            "description": description,
            "type": "pet_collection",
            "target": target,
            "target_value": 1,
            "config": json.dumps(config) if config else None,
            "difficulty": difficulty,
            "default_points": points,
        }
        row = existing.get(name.lower())
        if row is None:
            session.add(EventTaskLibraryItem(
                source=SOURCE, group_id=None, visibility="public", active=True, **mapped))
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
    print(f"pet task seed: {created} created, {updated} updated (source={SOURCE})")
