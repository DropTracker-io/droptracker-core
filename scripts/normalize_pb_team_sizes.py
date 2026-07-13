"""Normalize garbage personal_best.team_size encodings (suggestion #50).

The adventure-log PB parser stored raw fragments — ``"(2"``, ``"(5"``,
``"3 s"``, ``"11-15 s"``, ``"0"`` — alongside the canonical encodings
(``"Solo"``, ``"2"``, ``"11-15"``, …), splitting one team size into parallel
PB boards on the site. Intake now sanitizes at the source
(``utils.npc_names.sanitize_team_size``); this script repairs existing rows:

  1. Rewrite each dirty value to its sanitized form.
  2. Where a (player, npc, team_size) then has several rows (the player had
     PBs under both the dirty and clean spelling), keep the fastest.

Usage
-----
    python -m scripts.normalize_pb_team_sizes           # dry run (default)
    python -m scripts.normalize_pb_team_sizes --apply   # write changes

Idempotent: sanitized values sanitize to themselves.
"""

import argparse
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.npc_names import sanitize_team_size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    s = Session()
    values = [v for (v,) in s.execute(
        text("SELECT DISTINCT team_size FROM personal_best")).fetchall()]
    mapping = {v: sanitize_team_size(v) for v in values}
    dirty = {v: cv for v, cv in mapping.items() if cv != v}
    if not dirty:
        print(f"[{mode}] nothing to normalize.")
        s.close()
        return

    # Which (player, npc, clean_size) triples will need a fastest-only dedupe.
    affected = set()
    total_rows = 0
    for raw, clean in sorted(dirty.items()):
        rows = s.execute(text(
            "SELECT id, player_id, npc_id FROM personal_best WHERE team_size = :ts"
        ), {"ts": raw}).fetchall()
        total_rows += len(rows)
        print(f"[{mode}] {raw!r} -> {clean!r}: {len(rows)} rows")
        for _rid, pid, nid in rows:
            affected.add((pid, nid, clean))
        if args.apply:
            s.execute(text("UPDATE personal_best SET team_size = :clean WHERE team_size = :raw"),
                      {"clean": clean, "raw": raw})

    removed = 0
    collisions = 0
    for pid, nid, ts in sorted(affected):
        rows = s.execute(text(
            "SELECT id, personal_best, kill_time FROM personal_best "
            "WHERE player_id=:p AND npc_id=:n AND team_size=:t"
        ), {"p": pid, "n": nid, "t": ts}).fetchall()
        if len(rows) <= 1:
            continue
        collisions += 1
        ranked = sorted(rows, key=lambda r: (
            0 if (r[1] or 0) > 0 else 1, r[1] or float("inf"), r[2] or float("inf"), r[0]))
        for row_id, _, _ in ranked[1:]:
            removed += 1
            if args.apply:
                s.execute(text("DELETE FROM personal_best WHERE id = :i"), {"i": row_id})

    if args.apply:
        s.commit()
    s.close()
    note = "" if args.apply else " (dedupe counts are pre-rewrite estimates)"
    print(f"\n[{mode}] {total_rows} rows normalized across {len(dirty)} dirty values; "
          f"{collisions} (player, npc, size) collisions, {removed} slower duplicates removed{note}.")


if __name__ == "__main__":
    main()
