#!/usr/bin/env python3
"""Seed npc_list rows for plugin "activity" loot sources.

The plugin (>= 5.5.0) submits synthetic type=drop events for activities that
never fire a RuneLite loot event — MTA reward-shop purchases, Agility Pyramid
tops, deep sea trawling catches. `ensure_npc_id_for_player` rejects any drop
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
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed npc_list rows for plugin activity sources.")
    parser.add_argument("--apply", action="store_true", help="Write the rows (default: dry run)")
    args = parser.parse_args()

    to_create = []
    for name, npc_id in ACTIVITY_SOURCES:
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
