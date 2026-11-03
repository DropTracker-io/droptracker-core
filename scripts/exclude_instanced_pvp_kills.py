"""Exclude PvP kills recorded in instanced regions (2026-11-03).

Until ``utils.death_regions.pvp_minigame_for_region`` learned to treat any
instanced region as a minigame, LMS kills reached the PvP boards: the plugin's
kill handler reports the instance region (e.g. 25984) rather than the LMS map
region, so the LMS list never matched. Intake now excludes them; this marks
the kills already stored the same way (``excluded_reason =
'pvp_minigame:instanced'``), which takes them off every standing, board and
total. Hidden and already-excluded kills are left alone.

Afterwards rebuild the Redis standings:
    ./venv/bin/python scripts/rebuild_pvp_standings.py --apply

Usage
-----
    ./venv/bin/python -m scripts.exclude_instanced_pvp_kills          # dry run
    ./venv/bin/python -m scripts.exclude_instanced_pvp_kills --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text  # noqa: E402

# region_id >= 100 << 8 is map x >= 6400 (utils.death_regions.INSTANCE_REGION_X).
_MIN_INSTANCE_REGION = 100 << 8


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    from db.models import Session
    from utils.death_regions import PVP_INSTANCED, pvp_minigame_for_region

    reason = f"pvp_minigame:{PVP_INSTANCED}"
    with Session() as session:
        rows = session.execute(text(
            "SELECT id, region_id, world, total_value FROM pvp_kills "
            "WHERE excluded_reason IS NULL AND region_id >= :lo"
        ), {"lo": _MIN_INSTANCE_REGION}).fetchall()
        # The same rule intake uses, so the backfill can never disagree with it.
        ids = [r[0] for r in rows if pvp_minigame_for_region(r[1]) == PVP_INSTANCED]
        by_world: dict = {}
        for r in rows:
            if r[0] in ids:
                count, value = by_world.get(r[2], (0, 0))
                by_world[r[2]] = (count + 1, value + int(r[3] or 0))
        print(f"{len(ids)} kill(s) in instanced regions, "
              f"{sum(v for _, v in by_world.values()):,} gp")
        for world, (count, value) in sorted(by_world.items(), key=lambda kv: -kv[1][0]):
            print(f"  world {world}: {count} kill(s), {value:,} gp")
        if not args.apply:
            print("[DRY RUN] nothing written; pass --apply")
            return 0
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            session.execute(
                text("UPDATE pvp_kills SET excluded_reason = :reason "
                     "WHERE id IN :ids AND excluded_reason IS NULL")
                .bindparams(bindparam("ids", expanding=True)),
                {"reason": reason, "ids": batch},
            )
        session.commit()
        print(f"excluded {len(ids)} kill(s). Now run scripts/rebuild_pvp_standings.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
