"""Clamp personal_best.team_size to the team the boss can actually be fought in
(suggestion #140).

Plugin 6.0 (commit 9c259fc, 2026-08-04) started bracketing raid PBs by the
accumulated ``NearbyPlayerTracker`` roster instead of the health-orb varbits.
That roster is not bounded to a single raid, so names from consecutive runs
piled up and Theatre of Blood times — a five-player raid — were submitted as 6-,
7-, 8- and 9-player ones, with Tombs of Amascut (eight slots) reaching 15. The
Hall of Fame renders one board per distinct ``team_size``, so each phantom
bucket became a visible board.

Intake now clamps at the source (``utils.npc_names.clamp_team_size``); this
script repairs the rows written before that, mirroring
``scripts/normalize_pb_team_sizes.py``:

  1. Rewrite every over-cap (npc, team_size) pair to the boss's ceiling.
  2. Where a (player, npc, team_size) then holds several rows — the player also
     has a genuine row at the ceiling — keep the fastest.

Usage
-----
    python -m scripts.cap_pb_team_sizes            # dry run (default)
    python -m scripts.cap_pb_team_sizes --apply    # write changes

Idempotent: capped values clamp to themselves. Only ``personal_best`` is
touched; the seasonal mirror carries no team_size column.
"""

import argparse
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.npc_names import clamp_team_size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    s = Session()
    pairs = s.execute(text(
        "SELECT DISTINCT pb.npc_id, n.npc_name, pb.team_size "
        "FROM personal_best pb JOIN npc_list n ON n.npc_id = pb.npc_id"
    )).fetchall()

    over_cap = []
    for npc_id, npc_name, team_size in pairs:
        capped = clamp_team_size(npc_name, team_size)
        if capped != team_size:
            over_cap.append((npc_id, npc_name, team_size, capped))

    if not over_cap:
        print(f"[{mode}] no personal_best rows exceed their boss's team ceiling.")
        s.close()
        return

    affected = set()
    total_rows = 0
    for npc_id, npc_name, raw, capped in sorted(over_cap, key=lambda r: (r[1], r[2])):
        rows = s.execute(text(
            "SELECT id, player_id FROM personal_best "
            "WHERE npc_id = :n AND team_size = :ts"
        ), {"n": npc_id, "ts": raw}).fetchall()
        total_rows += len(rows)
        print(f"[{mode}] {npc_name}: {raw!r} -> {capped!r}: {len(rows)} rows")
        for _rid, pid in rows:
            affected.add((pid, npc_id, capped))
        if args.apply:
            s.execute(text(
                "UPDATE personal_best SET team_size = :capped "
                "WHERE npc_id = :n AND team_size = :raw"
            ), {"capped": capped, "n": npc_id, "raw": raw})

    removed = 0
    collisions = 0
    npc_names = {npc_id: npc_name for npc_id, npc_name, _raw, _capped in over_cap}
    for pid, nid, ts in sorted(affected):
        # Clamp in Python rather than filtering on team_size: in a dry run the
        # rewrite has not happened, so the rows that are about to collide still
        # carry their old over-cap values and a WHERE team_size=:t would miss
        # them (and report zero collisions for every run).
        rows = [
            r for r in s.execute(text(
                "SELECT id, personal_best, kill_time, team_size FROM personal_best "
                "WHERE player_id=:p AND npc_id=:n"
            ), {"p": pid, "n": nid}).fetchall()
            if clamp_team_size(npc_names.get(nid), r[3]) == ts
        ]
        if len(rows) <= 1:
            continue
        collisions += 1
        # Fastest wins; a zero/NULL personal_best sorts last (never a real time).
        ranked = sorted(rows, key=lambda r: (
            0 if (r[1] or 0) > 0 else 1, r[1] or float("inf"), r[2] or float("inf"), r[0]))
        for row_id, _, _, _ in ranked[1:]:
            removed += 1
            if args.apply:
                s.execute(text("DELETE FROM personal_best WHERE id = :i"), {"i": row_id})

    if args.apply:
        s.commit()
    s.close()
    print(f"\n[{mode}] {total_rows} rows capped across {len(over_cap)} (boss, size) pairs; "
          f"{collisions} (player, npc, size) collisions, {removed} slower duplicates removed.")


if __name__ == "__main__":
    main()
