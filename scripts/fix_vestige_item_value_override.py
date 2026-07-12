"""Fix vestige item_value_overrides: subtract the base ring as well as ingots.

The initial seed used:
    completed_ring − 3 × Chromium ingot

Correct formula:
    completed_ring − 3 × Chromium ingot − base_ring

Only touches the four ancient vestige rows (28279, 28281, 28283, 28285).
Idempotent — safe to re-run.

Run:
    venv/bin/python -m scripts.fix_vestige_item_value_overrides
    venv/bin/python -m scripts.fix_vestige_item_value_overrides --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from db.models import ItemList, ItemValueOverride, session
from utils.ge_value import get_mapping

# vestige_id → (completed_ring, base_ring)
# NOTE: these ids do NOT run Bellator/Ultor/Magus/Venator in that order in game
# (28281 is Magus vestige, 28283 is Venator vestige, 28285 is Ultor vestige) —
# verified against the `items` table, which is the source of truth. A prior
# version of this mapping assumed sequential order and transposed three of the
# four pairs. "Archers ring" (no apostrophe) matches the live GE mapping name.
VESTIGE_BASE_RING: dict[str, tuple[str, str]] = {
    "28279": ("Bellator ring", "Warrior ring"),
    "28281": ("Magus ring", "Seers ring"),
    "28283": ("Venator ring", "Archers ring"),
    "28285": ("Ultor ring", "Berserker ring"),
}

DESCRIPTION = (
    "The completed ring's value minus 3 Chromium ingots and the base ring."
)


async def _load_maps() -> tuple[dict[str, str], dict[str, int]]:
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


def _components_json(
    completed: str,
    base: str,
    name_to_id: dict[str, int],
) -> str:
    specs = [
        (completed, 1),
        ("Chromium ingot", -3),
        (base, -1),
    ]
    return json.dumps(
        [
            {
                "item_id": name_to_id.get(name.lower()),
                "item_name": name,
                "quantity": qty,
            }
            for name, qty in specs
        ]
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    id_to_name, name_to_id = asyncio.run(_load_maps())
    if not id_to_name:
        print("WARN: GE mapping unavailable — component item_ids may be null.", file=sys.stderr)

    s = session
    updated = missing = 0

    for vestige_id, (completed, base) in VESTIGE_BASE_RING.items():
        row = s.query(ItemValueOverride).filter(
            ItemValueOverride.item_id == int(vestige_id)
        ).first()
        if row is None:
            print(f"  [skip] {vestige_id}: no override row found")
            missing += 1
            continue

        # Vestiges are untradeable, so the GE mapping never has their name —
        # prefer the local `items` table (the true source for OSRS item ids).
        local = s.query(ItemList).filter(ItemList.item_id == int(vestige_id)).first()
        item_name = id_to_name.get(vestige_id) or (local.item_name if local else None) or row.item_name
        components = _components_json(completed, base, name_to_id)

        row.item_name = item_name
        row.divisor = 1
        row.flat_bonus = 0
        row.fallback_value = 0
        row.components = components
        row.description = DESCRIPTION
        row.active = True
        updated += 1

        formula = f"+1×{completed} −3×Chromium ingot −1×{base}"
        print(f"  [update] {vestige_id:>6} {item_name:<24} = {formula}")

    if args.dry_run:
        s.rollback()
        print(f"\ndry-run: rolled back ({updated} would update, {missing} missing)")
        return 0

    s.commit()
    from utils import value_overrides
    value_overrides.invalidate()
    print(f"\nfixed {updated} vestige rows. Cache invalidated.")
    if missing:
        print(f"WARN: {missing} vestige ids had no row — run seed script first?", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())