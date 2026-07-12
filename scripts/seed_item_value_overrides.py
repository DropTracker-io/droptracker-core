"""Seed ``item_value_overrides`` from the previously hard-coded valuation rules.

Reproduces, as editable table rows, the six "component of X, worth Y" families
that used to live in ``utils/ge_value.py`` and the id list in ``/value_mods``:

    * Hydra pieces        → 1/3 of a Brimstone ring
    * Abyssal bludgeon    → 1/3 of an Abyssal bludgeon
    * Ancient vestiges    → completed ring − 3 Chromium ingots
    * Noxious halberd     → 1/3 of a Noxious halberd
    * Araxyte fang        → Amulet of rancour − Amulet of torture
    * Mokhaiotl cloth     → gauntlets − bracelet − 10000 demon tears (else 5M)

The script is driven by the authoritative item-id groups the plugin already
uses (the old ``/value_mods`` lists), so the derived ``/value_mods`` endpoint
stays byte-for-byte identical after seeding. Item names (and each vestige's
matching ring) are resolved from the live GE mapping; the vestige→ring pairing
uses the same ``"vestige"→"ring"`` name replacement the old code did, so it is
correct regardless of id order. Component prices are still looked up by name at
runtime, so seeding works even if some ids can't be resolved.

Idempotent: upserts by ``item_id`` (then ``item_name``). Safe to re-run — e.g.
after the GE mapping recovers, to backfill any names it couldn't resolve.

Run:
    venv/bin/python -m scripts.seed_item_value_overrides            # apply
    venv/bin/python -m scripts.seed_item_value_overrides --dry-run  # preview
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

from db.models import ItemValueOverride, session
from utils.ge_value import get_mapping

# Sentinel: this group's ring component is derived per-item from the item name.
VESTIGE = "__vestige__"

# (item_ids, component_spec, divisor, fallback_value, description)
# component_spec is a list of (component_name, quantity) or the VESTIGE sentinel.
# item_ids mirror the old hard-coded /value_mods lists exactly.
GROUPS: list[tuple[list[str], object, int, int, str]] = [
    (
        ["22968", "22970", "22972", "22974"],
        [("Brimstone ring", 1)],
        3, 0,
        "One third of a Brimstone ring.",
    ),
    (
        ["13274", "13275", "13276", "18633", "18634", "18635"],
        [("Abyssal bludgeon", 1)],
        3, 0,
        "One third of an Abyssal bludgeon.",
    ),
    (
        ["28279", "28281", "28283", "28285"],
        VESTIGE,
        1, 0,
        "The completed ring's value minus 3 Chromium ingots.",
    ),
    (
        ["29790", "29792", "29794"],
        [("Noxious halberd", 1)],
        3, 0,
        "One third of a Noxious halberd.",
    ),
    (
        ["29799"],
        [("Amulet of rancour", 1), ("Amulet of torture", -1)],
        1, 0,
        "An Amulet of rancour minus an Amulet of torture.",
    ),
    (
        ["31109"],
        [("Confliction gauntlets", 1), ("Tormented bracelet", -1), ("Demon tear", -10000)],
        1, 5_000_000,
        "Confliction gauntlets minus a Tormented bracelet and 10,000 Demon tears.",
    ),
]

# Offline fallback for the vestige→ring pairing, used only when the GE mapping
# can't be fetched at seed time.
VESTIGE_RING_FALLBACK = {
    "28279": "Bellator ring",
    "28281": "Ultor ring",
    "28283": "Magus ring",
    "28285": "Venator ring",
}


async def _load_maps() -> tuple[dict[str, str], dict[str, int]]:
    """Return (id→name, name_lower→id) from the live GE mapping."""
    mapping = await get_mapping()
    id_to_name: dict[str, str] = {}
    name_to_id: dict[str, int] = {}
    if mapping:
        for it in mapping:
            iid = it.get("id")
            name = (it.get("name") or "").strip()
            if iid is None or not name:
                continue
            id_to_name[str(iid)] = name
            name_to_id.setdefault(name.lower(), iid)
    return id_to_name, name_to_id


def _component_specs(spec, item_id: str, item_name: str | None) -> list[tuple[str, int]]:
    """Resolve a group's component spec for one item (handles the vestige case)."""
    if spec != VESTIGE:
        return list(spec)
    # Derive the ring from the vestige's own name, exactly like the old code.
    ring = None
    if item_name and "vestige" in item_name.lower():
        ring = re.sub(r"vestige", "ring", item_name, flags=re.IGNORECASE).strip()
    if not ring:
        ring = VESTIGE_RING_FALLBACK.get(item_id)
    specs: list[tuple[str, int]] = []
    if ring:
        specs.append((ring, 1))
    specs.append(("Chromium ingot", -3))
    return specs


def _components_json(specs: list[tuple[str, int]], name_to_id: dict[str, int]) -> str:
    return json.dumps(
        [
            {"item_id": name_to_id.get(name.lower()), "item_name": name, "quantity": qty}
            for name, qty in specs
        ]
    )


def _upsert(s, item_id: str, item_name: str, components: str, divisor: int,
            fallback: int, description: str) -> str:
    iid = int(item_id)
    row = s.query(ItemValueOverride).filter(ItemValueOverride.item_id == iid).first()
    if row is None and item_name:
        row = s.query(ItemValueOverride).filter(ItemValueOverride.item_name == item_name).first()
    action = "update" if row is not None else "create"
    if row is None:
        row = ItemValueOverride(item_id=iid, item_name=item_name)
        s.add(row)
    row.item_id = iid
    row.item_name = item_name
    row.divisor = divisor
    row.flat_bonus = 0
    row.fallback_value = fallback
    row.components = components
    row.description = description
    row.active = True
    return action


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = ap.parse_args()

    id_to_name, name_to_id = asyncio.run(_load_maps())
    if not id_to_name:
        print(
            "WARN: GE mapping unavailable — seeding with fallback names and no "
            "component ids. Re-run when online to backfill.",
            file=sys.stderr,
        )

    s = session
    created = updated = 0
    for ids, spec, divisor, fallback, description in GROUPS:
        for item_id in ids:
            item_name = id_to_name.get(item_id) or f"Item {item_id}"
            specs = _component_specs(spec, item_id, id_to_name.get(item_id))
            components = _components_json(specs, name_to_id)
            action = _upsert(s, item_id, item_name, components, divisor, fallback, description)
            created += action == "create"
            updated += action == "update"
            formula = " ".join(f"{q:+d}×{n}" for n, q in specs)
            print(f"  [{action}] {item_id:>6} {item_name:<22} = ({formula}) / {divisor}"
                  + (f"  else {fallback}" if fallback else ""))

    if args.dry_run:
        s.rollback()
        print(f"\ndry-run: rolled back ({created} would be created, {updated} updated)")
        return 0

    s.commit()
    # Evict the runtime cache so live services pick up the seed without a restart.
    from utils import value_overrides
    value_overrides.invalidate()
    print(f"\nseeded {created} new, {updated} updated. Cache invalidated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
