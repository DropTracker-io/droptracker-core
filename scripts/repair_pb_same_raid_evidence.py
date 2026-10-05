"""Repair 2026-08-26 → 09-23 PB rows that a raid-mate proves were rounded down.

Between the tick backfill (2026-08-26) and the snap-direction fix (2026-09-23
13:09 UTC, ticket #182) intake snapped a whole-second time to the *nearest*
tick. For a display of ``S`` seconds with two ticks in its window that picks
the lower one, so a player with precise timing off was credited 600 ms faster
than they could have been. ``repair_pb_snap_direction`` fixed the rows the
backfill had recorded, but rows written inside the window kept no record of
what was displayed, so on their own they can't be told apart from a genuine
precise time.

A raid-mate settles it. Under the old rule a whole-second display only ever
produced tick residues {0, 2, 3} (``(ms / 600) mod 5``), so a row at residue 4
in that window is a precise time. When two players finished the same raid
(same boss and team size, written within 60 s of each other; Solo boards are
left out, since two solo kills a minute apart are different kills), one holds
``x`` at residue 3 and the other ``x + 600`` at residue 4, the second time is
measured and the first is the same kill rounded down by the old rule. The
corrected rule credits it with ``x + 600``, exactly the raid-mate's time.

Usage
-----
    python -m scripts.repair_pb_same_raid_evidence           # dry run
    python -m scripts.repair_pb_same_raid_evidence --apply   # write changes

Safety
------
* Only rows still holding exactly the value written in the window are moved
  (``WHERE personal_best = :old``); a PB set since is never touched.
* Upwards only, by exactly one tick, so nobody gains a place.
* The repaired row is marked ``precise_timing = 0`` and its raid-mate's
  ``precise_timing = 1``, which is what the evidence shows.
* Every changed row is backed up to ``logs/`` as JSON before anything is
  written. Idempotent: a repaired row sits at residue 4 and no longer matches.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from db.models.base import Session  # noqa: E402

WINDOW_START = "2026-08-26 15:00:00"
WINDOW_END = "2026-09-23 13:10:00"
TICK_MS = 600
SAME_RAID_SECONDS = 60


def _residue(ms):
    if not ms or ms % TICK_MS:
        return None
    return (ms // TICK_MS) % 5


def find_repairs(s):
    rows = s.execute(
        text(
            "SET STATEMENT max_statement_time=60 FOR "
            "SELECT id, player_id, npc_id, team_size, personal_best, kill_time, date_added "
            "FROM personal_best WHERE date_added >= :start AND date_added < :end "
            "AND personal_best > 0 AND team_size <> 'Solo'"
        ),
        {"start": WINDOW_START, "end": WINDOW_END},
    ).fetchall()

    boards = collections.defaultdict(list)
    for r in rows:
        boards[(r.npc_id, r.team_size)].append(r)

    repairs = {}
    for lst in boards.values():
        lst.sort(key=lambda r: r.date_added)
        for i, a in enumerate(lst):
            for b in lst[i + 1:]:
                if (b.date_added - a.date_added).total_seconds() > SAME_RAID_SECONDS:
                    break
                if a.player_id == b.player_id:
                    continue
                for low, mate in ((a, b), (b, a)):
                    if (
                        _residue(low.personal_best) == 3
                        and _residue(mate.personal_best) == 4
                        and mate.personal_best - low.personal_best == TICK_MS
                    ):
                        repairs.setdefault(low.id, {"row": low, "mates": set()})["mates"].add(mate.id)
    return len(rows), repairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    s = Session()
    try:
        scanned, repairs = find_repairs(s)
        print(f"Scanned {scanned} rows written {WINDOW_START} → {WINDOW_END}")
        print(f"Rows a raid-mate proves were rounded down: {len(repairs)}")
        mate_ids = sorted({m for r in repairs.values() for m in r["mates"]})

        backup = []
        for rid, info in sorted(repairs.items()):
            r = info["row"]
            backup.append({
                "id": r.id, "player_id": r.player_id, "npc_id": r.npc_id,
                "team_size": r.team_size, "personal_best": r.personal_best,
                "new_personal_best": r.personal_best + TICK_MS,
                "kill_time": r.kill_time,
                "new_kill_time": r.kill_time + TICK_MS if r.kill_time == r.personal_best else r.kill_time,
                "date_added": r.date_added.isoformat() if r.date_added else None,
                "mates": sorted(info["mates"]),
            })
        for b in backup[:10]:
            print(f"  id={b['id']} npc={b['npc_id']} team={b['team_size']} "
                  f"{b['personal_best']} → {b['new_personal_best']} (mates {b['mates']})")

        if not args.apply:
            print("Dry run; nothing written. Re-run with --apply.")
            s.rollback()
            return

        os.makedirs("logs", exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = f"logs/pb_same_raid_repair_{stamp}.json"
        with open(path, "w") as fh:
            json.dump({"repairs": backup, "precise_mates": mate_ids}, fh, indent=1)
        print(f"Backup written to {path}")

        changed = 0
        for b in backup:
            res = s.execute(
                text(
                    "UPDATE personal_best SET personal_best = :new_pb, kill_time = :new_kill, "
                    "precise_timing = 0 WHERE id = :id AND personal_best = :old_pb"
                ),
                {"new_pb": b["new_personal_best"], "new_kill": b["new_kill_time"],
                 "id": b["id"], "old_pb": b["personal_best"]},
            )
            changed += res.rowcount
        marked = 0
        if mate_ids:
            res = s.execute(
                text("UPDATE personal_best SET precise_timing = 1 WHERE id IN :ids "
                     "AND precise_timing IS NULL").bindparams(
                    __import__("sqlalchemy").bindparam("ids", expanding=True)),
                {"ids": mate_ids},
            )
            marked = res.rowcount
        s.commit()
        print(f"Repaired {changed} rows (+{TICK_MS} ms); marked {marked} raid-mate rows precise.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
