"""Decoding of the loadout the plugin attaches to a personal best.

The wire format is a compact ``slot-itemId-quantity`` list because it travels in
a webhook embed field, which is capped at 1024 characters. Parsing lives here so
it is testable without a database and so the submission processor and any future
backfill share one definition.

Everything here treats its input as hostile: the encoded string arrives from a
client we do not control.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# An inventory is 28 slots and worn equipment 11-14 depending on how you count;
# the cap is generous enough for either and small enough to bound the work.
MAX_SLOTS = 64

# The largest slot index and item id worth believing. OSRS item ids are well
# under 100k; the ceiling exists so a malformed value cannot become a huge int.
MAX_SLOT_INDEX = 1000
MAX_ITEM_ID = 200_000
MAX_QUANTITY = 2_147_483_647


def parse_loadout(encoded: Any) -> List[Dict[str, int]]:
    """Decodes ``"0-11802-1,3-995-1000"`` into slot/item/quantity dicts.

    Malformed entries are skipped rather than failing the whole loadout: losing
    one slot is much better than losing the loadout (or the personal best it is
    attached to) because a client sent something odd.

    Returns an empty list for anything unparseable, which callers treat the same
    as "no loadout was sent".
    """
    if not isinstance(encoded, str):
        return []

    entries: List[Dict[str, int]] = []
    seen_slots = set()

    for chunk in encoded.split(","):
        if len(entries) >= MAX_SLOTS:
            break
        chunk = chunk.strip()
        if not chunk:
            continue

        parts = chunk.split("-")
        if len(parts) != 3:
            continue

        try:
            slot = int(parts[0])
            item_id = int(parts[1])
            quantity = int(parts[2])
        except (TypeError, ValueError):
            continue

        if not (0 <= slot <= MAX_SLOT_INDEX):
            continue
        if not (0 < item_id <= MAX_ITEM_ID):
            continue
        if not (0 < quantity <= MAX_QUANTITY):
            continue
        # A repeated slot means a corrupt payload; keep the first, which matches
        # the order the client wrote them in.
        if slot in seen_slots:
            continue

        seen_slots.add(slot)
        entries.append({"slot": slot, "item_id": item_id, "quantity": quantity})

    entries.sort(key=lambda e: e["slot"])
    return entries


def serialize_loadout(entries: List[Dict[str, int]]) -> Optional[str]:
    """Stores the decoded loadout as JSON, or None when there is nothing to store.

    None rather than ``"[]"`` so "no loadout sent" and "loadout of nothing" stay
    distinguishable in the database.
    """
    if not entries:
        return None
    return json.dumps(entries, separators=(",", ":"))


def loadout_from_json(raw: Optional[str]) -> List[Dict[str, int]]:
    """Reads a stored loadout back; [] when absent or unreadable."""
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []

    out = []
    for entry in loaded[:MAX_SLOTS]:
        if not isinstance(entry, dict):
            continue
        slot = entry.get("slot")
        item_id = entry.get("item_id")
        quantity = entry.get("quantity")
        if not all(isinstance(v, int) for v in (slot, item_id, quantity)):
            continue
        out.append({"slot": slot, "item_id": item_id, "quantity": quantity})
    return out
