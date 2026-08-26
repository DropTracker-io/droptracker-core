"""Snap stored personal-best times onto the game's 600ms tick grid.

OSRS measures every duration in game ticks, so a real kill time is always a
multiple of 600ms. A client with "precise timing" off prints whole seconds
instead, and the plugin passes that on verbatim, so one board ends up holding
two different quantizations of the same thing — and a rounded time can out-rank
a true one it never actually beat.

``utils.pb_time`` documents how the game's rounding rule was *measured* rather
than assumed, and why inverting it is safe to apply to every row:

  * every tick-aligned value is a fixed point, so a time a precise client could
    have produced is never disturbed;
  * nothing moves by more than 200ms;
  * the snap is monotonic, so it cannot reorder two times on a board.

Intake snaps at the source now (``data/submissions/pb.py``,
``adventure_log.py``, ``clan_broadcast.py``). This repairs the rows written
before that, covering both ``personal_best`` and ``kill_time``.

Usage
-----
    python -m scripts.snap_pb_times_to_ticks            # dry run (default)
    python -m scripts.snap_pb_times_to_ticks --apply    # write changes
    python -m scripts.snap_pb_times_to_ticks --limit N  # cap rows examined

Idempotent, by construction: a snapped value is already a fixed point. Every
changed row is backed up to ``logs/`` as JSON before anything is written.

Only ``personal_best`` is touched. The live ``seasonal_personal_best`` table has
an entirely different shape (``activity_id``/``time_score``, no ``team_size``)
than its ORM model claims, so it is deliberately left alone.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.pb_time import TICK_MS, snap_to_tick

CHUNK = 5000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="cap rows examined (0 = all)")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    s = Session()
    total = s.execute(text("SELECT COUNT(*) FROM personal_best")).scalar()
    print(f"[{mode}] examining {total} personal_best rows (tick = {TICK_MS}ms)")

    changes = []
    shifts = Counter()
    examined = 0
    last_id = 0
    while True:
        rows = s.execute(
            text(
                "SELECT id, npc_id, team_size, personal_best, kill_time FROM personal_best "
                "WHERE id > :last ORDER BY id LIMIT :chunk"
            ),
            {"last": last_id, "chunk": CHUNK},
        ).fetchall()
        if not rows:
            break
        for row_id, npc_id, team_size, pb_ms, kill_ms in rows:
            last_id = row_id
            examined += 1
            new_pb, new_kill = snap_to_tick(pb_ms), snap_to_tick(kill_ms)
            if new_pb == (pb_ms or 0) and new_kill == (kill_ms or 0):
                continue
            changes.append(
                {
                    "id": row_id,
                    "npc_id": npc_id,
                    "team_size": team_size,
                    "personal_best": pb_ms,
                    "kill_time": kill_ms,
                    "new_personal_best": new_pb,
                    "new_kill_time": new_kill,
                }
            )
            if new_pb != (pb_ms or 0):
                shifts[new_pb - (pb_ms or 0)] += 1
        if args.limit and examined >= args.limit:
            break

    print(f"[{mode}] {len(changes)} of {examined} rows need snapping "
          f"({100 * len(changes) / examined if examined else 0:.1f}%)")
    if shifts:
        print(f"[{mode}] personal_best displacement histogram:")
        for delta in sorted(shifts):
            print(f"    {delta:+5d} ms : {shifts[delta]:>6} rows")
        worst = max(abs(d) for d in shifts)
        print(f"[{mode}] largest displacement: {worst}ms "
              f"({'OK' if worst <= TICK_MS // 3 else 'UNEXPECTED — investigate'})")

    if not changes:
        print(f"[{mode}] nothing to do.")
        s.close()
        return

    os.makedirs("logs", exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup = f"logs/pb_tick_snap_{'apply' if args.apply else 'dryrun'}_{stamp}.json"
    with open(backup, "w") as fh:
        json.dump(changes, fh, indent=2, default=str)
    print(f"[{mode}] before/after for every changed row written to {backup}")

    if not args.apply:
        print("[DRY RUN] nothing was written. Re-run with --apply to commit.")
        s.close()
        return

    for i in range(0, len(changes), CHUNK):
        for change in changes[i:i + CHUNK]:
            s.execute(
                text("UPDATE personal_best SET personal_best=:pb, kill_time=:kt WHERE id=:i"),
                {"pb": change["new_personal_best"], "kt": change["new_kill_time"], "i": change["id"]},
            )
        s.commit()
        print(f"[APPLY] committed {min(i + CHUNK, len(changes))}/{len(changes)}")

    remaining = s.execute(
        text(
            "SELECT COUNT(*) FROM personal_best "
            "WHERE (personal_best > 0 AND personal_best %% :t <> 0) "
            "   OR (kill_time > 0 AND kill_time %% :t <> 0)"
        ),
        {"t": TICK_MS},
    ).scalar()
    print(f"[APPLY] done. Rows still off the tick grid: {remaining} (expected 0)")
    s.close()


if __name__ == "__main__":
    main()
