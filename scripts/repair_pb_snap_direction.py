"""Re-derive the 2026-08-26 tick backfill with the corrected snap direction.

Ticket #182 (Koeppy): two clan-mates ran one raid together, and the one with
precise timing *off* came out 600ms ahead on the board.

The cause was the direction of the snap, not the snap itself. A non-precise
client prints the true duration rounded to the nearest second, so a display of
``S`` seconds means the truth lay in ``[1000S-500, 1000S+500)`` — a window that
holds two ticks for two seconds out of every three. The original backfill and
intake resolved that to the *nearest* tick, which lands on the lower of the two
half the time and credits a player with a duration they never achieved.
``utils.pb_time`` now takes the slowest tick the display is consistent with.

Intake was fixed at the source, and every stored row is already tick-aligned,
so simply re-running ``scripts.snap_pb_times_to_ticks`` is a no-op: the
original display values are gone from the table. They survive only in the
backfill's own backup, which recorded every row's before/after. This script
replays that backup, recomputes each value under the corrected rule, and moves
only the rows the old rule pushed too fast.

Rows written *after* the backfill are not covered — their pre-snap display is
not recorded anywhere, and a stored value cannot be attributed to a precise or
a non-precise client after the fact. Only ``--report-recent`` measures them.

Usage
-----
    python -m scripts.repair_pb_snap_direction                  # dry run
    python -m scripts.repair_pb_snap_direction --apply          # write changes
    python -m scripts.repair_pb_snap_direction --report-recent  # post-backfill scope

Safety
------
* A row is skipped unless its stored value still equals exactly what the
  backfill left there. Anything else means a genuine PB landed since, and it
  must not be clobbered.
* Idempotent: a repaired value is a fixed point of the corrected rule.
* Monotonic and upwards-only, so no board is reordered in a player's favour.
* Every changed row is backed up to ``logs/`` as JSON before anything is
  written.
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from utils.pb_time import TICK_MS, snap_to_tick

CHUNK = 2000
FIELDS = (("personal_best", "new_personal_best"), ("kill_time", "new_kill_time"))


def newest_backup():
    found = sorted(glob.glob("logs/pb_tick_snap_apply_*.json"))
    if not found:
        sys.exit("no logs/pb_tick_snap_apply_*.json backup found — nothing to replay")
    return found[-1]


def report_recent(s, since):
    """Size the population this script cannot reach.

    Two players who finished the same raid must hold the same duration. Where
    they disagree by exactly one tick, one of them is a non-precise
    reconstruction that the old rule resolved the wrong way.
    """
    import itertools
    from collections import defaultdict

    rows = s.execute(
        text("SELECT npc_id, team_size, player_id, kill_time, date_added FROM personal_best "
             "WHERE kill_time > 0 AND date_added >= :since"),
        {"since": since},
    ).fetchall()

    buckets = defaultdict(list)
    for npc, team, pid, kill_ms, added in rows:
        buckets[(npc, team)].append((added, pid, kill_ms))

    pairs = agree = off_by_tick = 0
    for items in buckets.values():
        items.sort()
        group = [items[0]]
        groups = []
        for item in items[1:]:
            if (item[0] - group[-1][0]).total_seconds() <= 60:
                group.append(item)
            else:
                groups.append(group)
                group = [item]
        groups.append(group)
        for group in groups:
            for (_, p1, k1), (_, p2, k2) in itertools.combinations(group, 2):
                if p1 == p2:
                    continue
                pairs += 1
                if k1 == k2:
                    agree += 1
                elif abs(k1 - k2) == TICK_MS:
                    off_by_tick += 1

    print(f"[recent] rows since {since}: {len(rows)}")
    print(f"[recent] same-raid pairs: {pairs}; identical: {agree}; off by one tick: {off_by_tick}")
    print("[recent] these predate the intake fix and cannot be repaired from stored data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--backup", default=None, help="backfill backup to replay")
    parser.add_argument("--report-recent", action="store_true",
                        help="also size the post-backfill rows this cannot reach")
    parser.add_argument("--since", default="2026-08-26", help="cutoff for --report-recent")
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY RUN"

    path = args.backup or newest_backup()
    records = json.load(open(path))
    print(f"[{mode}] replaying {len(records)} rows from {path} (tick = {TICK_MS}ms)")

    s = Session()

    # What the corrected rule would have produced, per row and per field.
    wanted = {}
    for record in records:
        fixes = {}
        for original, applied in FIELDS:
            was = record[original]
            if was is None or was <= 0:
                continue
            correct = snap_to_tick(was)
            if correct != record[applied]:
                fixes[original] = (record[applied], correct)
        if fixes:
            wanted[record["id"]] = fixes
    print(f"[{mode}] {len(wanted)} rows were resolved the wrong way by the old rule")

    changes, shifts = [], Counter()
    skipped_missing = skipped_moved = 0
    ids = sorted(wanted)
    for i in range(0, len(ids), CHUNK):
        batch = ids[i:i + CHUNK]
        current = {
            row[0]: row
            for row in s.execute(
                text("SELECT id, personal_best, kill_time FROM personal_best WHERE id IN :ids"),
                {"ids": tuple(batch)},
            ).fetchall()
        }
        for row_id in batch:
            row = current.get(row_id)
            if row is None:
                skipped_missing += 1
                continue
            stored = {"personal_best": row[1], "kill_time": row[2]}
            change = {"id": row_id}
            for field, (left_by_backfill, correct) in wanted[row_id].items():
                # Only touch a value the backfill is still responsible for. A
                # different value means a real PB landed since 2026-08-26.
                if stored[field] != left_by_backfill:
                    continue
                change[field] = stored[field]
                change["new_" + field] = correct
                shifts[correct - stored[field]] += 1
            if len(change) == 1:
                skipped_moved += 1
                continue
            changes.append(change)

    print(f"[{mode}] {len(changes)} rows to repair "
          f"({skipped_moved} superseded by a later PB, {skipped_missing} no longer exist)")
    if shifts:
        print(f"[{mode}] displacement histogram:")
        for delta in sorted(shifts):
            print(f"    {delta:+5d} ms : {shifts[delta]:>6} values")
        if any(d <= 0 for d in shifts):
            sys.exit(f"[{mode}] ABORT: a repair moved a time downwards or nowhere — the "
                     "corrected rule must only ever slow a mis-resolved time")
        if max(shifts) > TICK_MS:
            sys.exit(f"[{mode}] ABORT: a repair moved a time by more than one tick")

    if not changes:
        print(f"[{mode}] nothing to do.")
        if args.report_recent:
            report_recent(s, args.since)
        s.close()
        return

    os.makedirs("logs", exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out = f"logs/pb_snap_direction_{'apply' if args.apply else 'dryrun'}_{stamp}.json"
    with open(out, "w") as fh:
        json.dump(changes, fh, indent=2, default=str)
    print(f"[{mode}] before/after for every changed row written to {out}")

    if not args.apply:
        print("[DRY RUN] nothing was written. Re-run with --apply to commit.")
        if args.report_recent:
            report_recent(s, args.since)
        s.close()
        return

    written = 0
    for i in range(0, len(changes), CHUNK):
        for change in changes[i:i + CHUNK]:
            sets, params = [], {"i": change["id"]}
            for field, _ in FIELDS:
                if field in change:
                    sets.append(f"{field}=:{field}")
                    params[field] = change["new_" + field]
            s.execute(text(f"UPDATE personal_best SET {', '.join(sets)} WHERE id=:i"), params)
            written += 1
        s.commit()
        print(f"[APPLY] committed {written}/{len(changes)}")

    off_grid = s.execute(
        text("SELECT COUNT(*) FROM personal_best WHERE personal_best > 0 "
             "AND MOD(personal_best, :tick) <> 0"),
        {"tick": TICK_MS},
    ).scalar()
    print(f"[APPLY] done. personal_best values off the tick grid: {off_grid}")
    if args.report_recent:
        report_recent(s, args.since)
    s.close()


if __name__ == "__main__":
    main()
