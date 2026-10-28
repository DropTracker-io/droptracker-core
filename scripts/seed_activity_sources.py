#!/usr/bin/env python3
"""Seed npc_list rows for loot sources that are not monsters.

Two kinds: plugin "activity" sources (below) and RuneLite loot-tracker events
(``RUNELITE_EVENT_SOURCES``).

The plugin (>= 5.5.0) submits synthetic type=drop events for activities that
never fire a RuneLite loot event — MTA reward-shop purchases, Agility Pyramid
tops, deep sea trawling catches, Pharaoh's sceptres. `ensure_npc_id_for_player` rejects any drop
whose source has no npc_list row (these aren't monsters, so the semantic wiki
lookup can't mint one), and event task-builder validation 422s on the names —
so the rows must exist before the plugin ships.

Ids sit in a reserved high range (900001+) so a real game npc id can never
collide. Expect these ids to 404 in best-effort NPC image backfills — that's
harmless (placeholder icon).

Idempotent: skips names that already exist (exact or variant-slug match) and
asserts the target ids are unused.

Usage:
    ./venv/bin/python3 -m scripts.seed_activity_sources           # dry run
    ./venv/bin/python3 -m scripts.seed_activity_sources --apply   # write
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text  # noqa: E402

from db import NpcList, session  # noqa: E402
from utils.npc_names import npc_match_variants, npc_slug_sql_expr  # noqa: E402

ACTIVITY_SOURCES = [
    ("Mage Training Arena", 900001),
    ("Agility Pyramid", 900002),
    ("Deep Sea Trawling", 900003),
    # Plugin >= 6.1.7 submits Pharaoh's sceptres under this source.
    ("Pyramid Plunder", 900004),
    # Server-side only: data/submissions/clog_drop_mirror.py records a new
    # eternal glory collection log slot as a drop from here.
    ("Fountain of Rune", 900005),
    # The Fractured Archive's reward chest: RuneLite 1.13.3 names the loot
    # event "Fractured Archive" (no article), and the chest has no monster
    # infobox for the wiki lookup to mint a row from.
    ("Fractured Archive", 900006),
]

# RuneLite's own loot-tracker event names (LootTrackerPlugin, checked against
# runelite master on 2026-10-25) that had no npc_list row. The plugin forwards
# every EVENT loot it sees, so each of these was rejected as an unknown NPC,
# one new_npc row per item: about 18k items in the week to 2026-10-25 (t382).
# They are chests, reward containers and skilling payouts, not monsters, so
# the wiki lookup can never mint them. "Loot Chest" (opened PvP loot keys) is
# deliberately absent: data/submissions/pvp.py takes it before this lookup.
# Ids come from list position: APPEND new names only, never insert or reorder.
RUNELITE_EVENT_SOURCES = [
    (name, 900007 + i) for i, name in enumerate([
        # Region- and object-based chests.
        "Rusty chest", "Tarnished chest", "Reinforced chest",
        "Chest (Alchemist's signet)", "Chest (Moon key)", "Chest (Ancient Vault)",
        "Dark Chest", "Larran's small chest",
        "H.A.M. chest (bronze)", "H.A.M. chest (iron)", "H.A.M. chest (steel)",
        "H.A.M. chest (silver)",
        # Taverley/Lumbridge key chests the existing rows do not cover.
        "Silver key black", "Black key red", "Black key brown", "Steel key black",
        # Activities and reward containers.
        "Golem Crafting", "Courier tasks", "Hallowed Sack", "Ore Pack (Volcanic Mine)",
        "Bag full of gems (Belona)",
        "Simple lockbox", "Elaborate lockbox", "Ornate lockbox",
        "Bounty crate (tier 1)", "Bounty crate (tier 2)", "Bounty crate (tier 3)",
        "Bounty crate (tier 4)", "Bounty crate (tier 5)", "Bounty crate (tier 6)",
        "Bounty crate (tier 7)", "Bounty crate (tier 8)", "Bounty crate (tier 9)",
        # Items cleaned or cracked open (named after the item's members name).
        "Tarnished 2h sword", "Tarnished amulet", "Tarnished battleaxe",
        "Tarnished halberd", "Tarnished longsword", "Tarnished necklace",
        "Tarnished ring", "Tarnished spear", "Tarnished bracelet",
        "Rubium geode", "Dirty arrowtips",
    ])
]

# Boss variants the wiki lookup cannot mint. The kill-count line names the
# awakened Whisperer "Whisperer (awakened)" (suggestion #199); the other three
# awakened DT2 bosses already have rows (13969, 13971, 13976). Own id block so
# the list above can keep appending.
BOSS_VARIANT_SOURCES = [
    ("Whisperer (Awakened)", 900201),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed npc_list rows for plugin activity sources.")
    parser.add_argument("--apply", action="store_true", help="Write the rows (default: dry run)")
    parser.add_argument("--only", action="append", metavar="NAME",
                        help="Seed only these names (repeatable; default: all)")
    args = parser.parse_args()

    sources = ACTIVITY_SOURCES + RUNELITE_EVENT_SOURCES + BOSS_VARIANT_SOURCES
    if args.only:
        wanted = {name.casefold() for name in args.only}
        sources = [(name, npc_id) for name, npc_id in sources if name.casefold() in wanted]
        if len(sources) != len(wanted):
            print(f"ABORT unknown --only name(s): {sorted(wanted - {n.casefold() for n, _ in sources})}")
            return 1

    to_create = []
    for name, npc_id in sources:
        existing = session.query(NpcList).filter(NpcList.npc_name == name).first()
        if existing:
            print(f"SKIP  {name!r}: already exists as npc_id={existing.npc_id}")
            continue
        # A variant-slug row ("The Mage Training Arena", odd casing, ...) would
        # split the source: ensure_npc_id_for_player's normalized fallback
        # resolves it instead of our row, or vice versa per intake path.
        variants = npc_match_variants(name)
        variant_row = session.execute(
            text(
                f"SELECT npc_id, npc_name FROM npc_list "
                f"WHERE {npc_slug_sql_expr('npc_name')} IN :variants LIMIT 1"
            ).bindparams(bindparam("variants", expanding=True)),
            {"variants": variants},
        ).first()
        if variant_row:
            print(f"SKIP  {name!r}: variant row exists — npc_id={variant_row.npc_id} ({variant_row.npc_name!r})")
            continue
        id_row = session.query(NpcList).filter(NpcList.npc_id == npc_id).first()
        if id_row:
            print(f"ABORT {name!r}: target npc_id={npc_id} already used by {id_row.npc_name!r}")
            return 1
        to_create.append((name, npc_id))

    if not to_create:
        print("Nothing to do.")
        return 0

    for name, npc_id in to_create:
        print(f"{'CREATE' if args.apply else 'WOULD CREATE'}  npc_id={npc_id}  npc_name={name!r}")

    if args.apply:
        for name, npc_id in to_create:
            session.add(NpcList(npc_id=npc_id, npc_name=name))
        session.commit()
        print(f"Committed {len(to_create)} row(s). The webhook consumer picks these up via its "
              f"DB fallback without a restart; restarting droptracker-webhook-consumer just "
              f"refreshes the in-memory cache immediately.")
    else:
        print("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
