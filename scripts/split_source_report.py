"""Report on the TEMP split-source observation data (services/split_observer).

Buckets every observed loot source by how often company shows up alongside
accepted plugin submissions:

  RAIDS         authoritative rosters (ToB/ToA/CoX) — split tracking candidates
  ACCOMPANIED   nearby players show up often — candidates for splits OR
                single-way areas where company is possible but splits are not
  SOLO          effectively always killed alone — no split logic needed
  MIXED         some company, below the accompanied threshold
  LOW DATA      not enough capable-version kills observed yet

Read-only over Redis; ``--pb-history`` adds a DB read of the personal_best
table (team-size distribution per timed boss — evidence that predates the
observation window). ``--csv`` dumps every source with raw counters for
spreadsheet triage.

Usage:
  ./venv/bin/python -m scripts.split_source_report
  ./venv/bin/python -m scripts.split_source_report --min-kills 30 --samples 3
  ./venv/bin/python -m scripts.split_source_report --csv /tmp/split_sources.csv --pb-history
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import split_observer as so  # noqa: E402

BUCKET_ORDER = [
    (so.BUCKET_RAID, "RAIDS — authoritative rosters being received"),
    (so.BUCKET_ACCOMPANIED, "OFTEN ACCOMPANIED — nearby players show up"),
    (so.BUCKET_SOLO, "ALWAYS SOLO — no company observed"),
    (so.BUCKET_MIXED, "MIXED — occasional company"),
    (so.BUCKET_LOW_DATA, "LOW DATA — not enough observed kills yet"),
]


def _pct(rate) -> str:
    return "-" if rate is None else f"{100.0 * rate:5.1f}%"


def _avg_nearby(stats) -> str:
    with_players = stats.get("with_players", 0) + stats.get("kt_with_players", 0)
    players_sum = stats.get("players_sum", 0) + stats.get("kt_players_sum", 0)
    if not with_players:
        return "-"
    return f"{players_sum / with_players:.1f}"


def _hist(stats) -> str:
    parts = []
    for label in ("n1", "n2", "n3", "n4", "n5plus"):
        total = stats.get(label, 0) + stats.get(f"kt_{label}", 0)
        if total:
            parts.append(f"{label[1:].replace('plus', '+')}:{total}")
    return " ".join(parts) or "-"


def _team_evidence(stats) -> str:
    reported = stats.get("kt_team_reported", 0)
    if not reported:
        return "-"
    gt1 = stats.get("kt_team_gt1", 0)
    avg = stats.get("kt_team_sum", 0) / reported
    return f"{gt1}/{reported} team (avg {avg:.1f})"


def _row_sort_key(item):
    npc_id, stats, _bucket, rate = item
    capable = stats.get("capable", 0) + stats.get("kt_capable", 0)
    return (-(rate or 0), -capable, npc_id)


def build_rows(snap, args):
    rows = []
    for npc_id, stats in snap["npcs"].items():
        bucket, rate = so.classify(
            stats, min_kills=args.min_kills,
            nearby_min=args.nearby_threshold, solo_max=args.solo_threshold,
        )
        rows.append((npc_id, stats, bucket, rate))
    return rows


def print_report(rows, snap, args):
    started = snap.get("started")
    started_txt = (
        datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if started else "unknown"
    )
    print(f"Split-source observation report — collecting since {started_txt}")
    print(f"Sources observed: {len(rows)}   (thresholds: min-kills={args.min_kills}, "
          f"accompanied>={args.nearby_threshold:.0%}, solo<={args.solo_threshold:.0%})")

    for bucket, title in BUCKET_ORDER:
        bucket_rows = sorted([r for r in rows if r[2] == bucket], key=_row_sort_key)
        if not bucket_rows:
            continue
        print(f"\n=== {title} ({len(bucket_rows)}) ===")
        header = (f"{'npc_id':>7}  {'source':<38} {'kills':>6} {'capable':>7} "
                  f"{'nearby%':>7} {'avg±':>5}  {'sizes':<18} {'kt team-size':<22}")
        print(header)
        print("-" * len(header))
        for npc_id, stats, _b, rate in bucket_rows:
            kills = stats.get("kills", 0) + stats.get("kt_kills", 0)
            capable = stats.get("capable", 0) + stats.get("kt_capable", 0)
            print(f"{npc_id:>7}  {stats.get('name', '')[:38]:<38} {kills:>6} {capable:>7} "
                  f"{_pct(rate):>7} {_avg_nearby(stats):>5}  {_hist(stats):<18} "
                  f"{_team_evidence(stats):<22}")
        if args.samples and bucket in (so.BUCKET_RAID, so.BUCKET_ACCOMPANIED, so.BUCKET_MIXED):
            for npc_id, stats, _b, _r in bucket_rows[: args.sample_sources]:
                samples = so.get_samples(npc_id, limit=args.samples)
                if not samples:
                    continue
                print(f"  · {stats.get('name', npc_id)} rosters:")
                for s in samples:
                    names = ", ".join(s.get("n", []))
                    print(f"      [{s.get('k', '?')}] {s.get('p', '?')} + [{names}]")


def write_csv(rows, path):
    fields = ["npc_id", "name", "bucket", "nearby_rate", "kills", "capable",
              "with_players", "players_sum", "drops", "kt_kills", "kt_capable",
              "kt_with_players", "kt_players_sum", "kt_team_reported",
              "kt_team_gt1", "kt_team_sum", "n1", "n2", "n3", "n4", "n5plus",
              "last_seen"]
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for npc_id, stats, bucket, rate in sorted(rows, key=_row_sort_key):
            writer.writerow([
                npc_id, stats.get("name", ""), bucket,
                "" if rate is None else f"{rate:.4f}",
            ] + [stats.get(f, 0) for f in fields[4:]])
    print(f"\nCSV written to {path}")


def print_pb_history():
    """Team-size distribution per timed boss from the personal_best table."""
    from sqlalchemy import text
    from db.models.base import session

    rows = session.execute(text(
        """
        SELECT n.npc_id, n.npc_name, p.team_size, COUNT(*) AS players
        FROM personal_best p
        JOIN npc_list n ON n.npc_id = p.npc_id
        GROUP BY n.npc_id, n.npc_name, p.team_size
        """
    )).fetchall()
    by_npc = {}
    for npc_id, npc_name, team_size, players in rows:
        entry = by_npc.setdefault(npc_id, {"name": npc_name, "sizes": {}})
        entry["sizes"][str(team_size)] = int(players)

    print("\n=== PB HISTORY — players holding a PB per team size (all-time DB) ===")
    print(f"{'npc_id':>7}  {'boss':<38} {'solo':>6} {'team':>6}  sizes")
    for npc_id in sorted(by_npc, key=lambda i: -sum(by_npc[i]["sizes"].values())):
        entry = by_npc[npc_id]
        solo = entry["sizes"].get("Solo", 0)
        team = sum(v for k, v in entry["sizes"].items() if k != "Solo")
        sizes = " ".join(
            f"{k}:{v}" for k, v in sorted(entry["sizes"].items(),
                                          key=lambda kv: (kv[0] != "Solo", kv[0]))
        )
        print(f"{npc_id:>7}  {str(entry['name'])[:38]:<38} {solo:>6} {team:>6}  {sizes}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--min-kills", type=int, default=20,
                        help="capable-version kills required before bucketing (default 20)")
    parser.add_argument("--nearby-threshold", type=float, default=0.10,
                        help="nearby rate at/above which a source is ACCOMPANIED (default 0.10)")
    parser.add_argument("--solo-threshold", type=float, default=0.02,
                        help="nearby rate at/below which a source is SOLO (default 0.02)")
    parser.add_argument("--samples", type=int, default=2,
                        help="roster samples to print per source (0 to disable)")
    parser.add_argument("--sample-sources", type=int, default=8,
                        help="how many sources per bucket get sample rosters printed")
    parser.add_argument("--csv", metavar="PATH", help="also dump every source to CSV")
    parser.add_argument("--pb-history", action="store_true",
                        help="add team-size distribution from the personal_best table (DB read)")
    args = parser.parse_args()

    snap = so.snapshot()
    rows = build_rows(snap, args)
    if not rows:
        print("No observations recorded yet — is droptracker-webhook-consumer "
              "running with the split_observer taps deployed?")
    else:
        print_report(rows, snap, args)
        if args.csv:
            write_csv(rows, args.csv)
    if args.pb_history:
        print_pb_history()


if __name__ == "__main__":
    main()
