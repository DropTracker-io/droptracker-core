"""Move recorded collection log rows off name-derived ids onto the real slot.

A full collection log read reports ids straight from the game and is always
right. A *single* unlock is announced by a chat message carrying only the item's
name, which the plugin resolves against RuneLite's item cache — and that returns
the earliest id sharing the name, which for a duplicated name is the wrong item.
The collection log's Coal bag is 25627; the item cache answers 764.

The unlock itself is not in doubt — the player did fill that slot — so these
rows are moved rather than deleted. ``/state/sync`` does the same repair on the
way in (``services.state_sync.repair_name_derived_items``), so this is only for
rows recorded before that existed.

Only ids the structure cannot place are touched, and only where the structure
has **exactly one** slot of that name: twenty-three names (every Graceful piece,
all the decorative armour) sit on several slots at once, and a name cannot say
which of them a player unlocked. Anything ambiguous, and anything the structure
simply does not know, is left alone — an unexplained id is more likely a game
update we have not ingested than a mistake.

Refresh the structure first, or a genuinely-new slot looks like a wrong id:

    ./venv/bin/python -m scripts.sync_collection_log --refresh --apply
    ./venv/bin/python -m scripts.repair_name_derived_clog_items
    ./venv/bin/python -m scripts.repair_name_derived_clog_items --apply
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from scripts.sync_collection_log import (
    item_names_for,
    observed_slot_ids,
    slot_ids,
    slot_names,
    stored_structure,
)


def unique_slot_by_name(structure):
    """Lowercased slot name -> the one slot id with that name."""
    by_name = defaultdict(set)
    for item_id, name in slot_names(structure).items():
        if name and name.strip():
            by_name[name.strip().lower()].add(item_id)
    return {name: ids.pop() for name, ids in by_name.items() if len(ids) == 1}


def plan(session):
    """``(moves, unexplained)`` — what would change, and what is left over.

    ``moves`` is ``(wrong_id, slot_id, name, holders)``.
    """
    structure = stored_structure(session)
    if structure is None:
        return None, None

    defined = slot_ids(structure)
    by_name = unique_slot_by_name(structure)
    reported = observed_slot_ids(session)
    unknown = [item_id for item_id in reported if item_id not in defined]
    names = item_names_for(session, unknown)

    moves, unexplained = [], []
    for item_id in sorted(unknown, key=lambda i: -reported[i]):
        name = (names.get(item_id) or "").strip()
        slot_id = by_name.get(name.lower()) if name else None
        if slot_id is None or slot_id == item_id:
            unexplained.append((item_id, name, reported[item_id]))
        else:
            moves.append((item_id, slot_id, name, reported[item_id]))
    return moves, unexplained


def apply_moves(session, moves) -> int:
    """Move each player's row onto the slot id, merging where both exist."""
    from db.models import PlayerCollectionLogItem

    moved = 0
    for wrong_id, slot_id, _name, _holders in moves:
        rows = (
            session.query(PlayerCollectionLogItem)
            .filter(PlayerCollectionLogItem.item_id.in_([wrong_id, slot_id]))
            .all()
        )
        by_player = defaultdict(dict)
        for row in rows:
            by_player[row.player_id][row.item_id] = row

        for _player_id, owned in by_player.items():
            wrong_row = owned.get(wrong_id)
            if wrong_row is None:
                continue
            existing = owned.get(slot_id)
            if existing is None:
                # The primary key covers (player_id, item_id), so the id cannot
                # be reassigned in place — insert the slot, drop the wrong row.
                session.add(PlayerCollectionLogItem(
                    player_id=wrong_row.player_id,
                    item_id=slot_id,
                    quantity=wrong_row.quantity,
                    first_seen_at=wrong_row.first_seen_at,
                ))
            else:
                # The unlock path always says 1; a full read may have said 40.
                existing.quantity = max(existing.quantity, wrong_row.quantity)
            session.delete(wrong_row)
            moved += 1
    session.commit()
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()

    from db.models import Session

    session = Session()
    try:
        moves, unexplained = plan(session)
        if moves is None:
            print("No collection_log structure stored — run scripts.sync_collection_log first.")
            return 1

        if not moves:
            print("Nothing to repair: no recorded id resolves to a slot of the same name.")
        else:
            rows = sum(holders for _w, _s, _n, holders in moves)
            print(f"{len(moves)} ids to move, covering {rows} rows:")
            for wrong_id, slot_id, name, holders in moves:
                print(f"  {wrong_id:>7} -> {slot_id:<7} {name:<28} {holders} accounts")

        if unexplained:
            print(f"\n{len(unexplained)} recorded ids left alone (the structure cannot "
                  f"place them, or the name is on several slots):")
            for item_id, name, holders in unexplained:
                print(f"  {item_id:>7}  {name or '?':<28} {holders} accounts")

        if not args.apply:
            print("\nDry run — re-run with --apply to write.")
            return 0

        moved = apply_moves(session, moves)
        print(f"\nMoved {moved} rows.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
