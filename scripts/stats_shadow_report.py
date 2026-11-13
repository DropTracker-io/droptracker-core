#!/usr/bin/env python3
"""Shadow-test report: how our stat numbers compare with Wise Old Man's.

    venv/bin/python -m scripts.stats_shadow_report                     # every consumer
    venv/bin/python -m scripts.stats_shadow_report --consumer events   # one, with its worst rows
    venv/bin/python -m scripts.stats_shadow_report --consumer events --context event:160 --worst 40

For each consumer and context it prints how many (player, metric) pairs were
compared, how many matched, how many we had no data for (coverage), and the
largest differences. Read-only. The rows come from ``stats_shadow_diffs``,
written while a consumer's switch is ``shadow`` (see utils/stats_shadow.py).
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict


def is_partial(row) -> bool:
    return str(getattr(row, "our_source", "") or "").endswith("/partial")


def summarise(rows, tolerance_for) -> dict:
    """``{(consumer, context): {...counts...}}`` from StatsShadowDiff-like rows."""
    out = defaultdict(lambda: {"rows": 0, "compared": 0, "matched": 0, "no_data_ours": 0,
                               "no_data_wom": 0, "partial": 0, "players": set(), "last_seen": None})
    for row in rows:
        bucket = out[(row.consumer, row.context)]
        bucket["rows"] += 1
        if row.player_id:
            bucket["players"].add(row.player_id)
        if bucket["last_seen"] is None or row.last_seen > bucket["last_seen"]:
            bucket["last_seen"] = row.last_seen
        if row.our_value is None:
            bucket["no_data_ours"] += 1
        elif is_partial(row):
            # Our first reading came well after the window opened: not a fair pair.
            bucket["partial"] += 1
        elif row.wom_value is None:
            bucket["no_data_wom"] += 1
        else:
            bucket["compared"] += 1
            if abs(row.our_value - row.wom_value) <= tolerance_for(row.consumer):
                bucket["matched"] += 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--consumer")
    parser.add_argument("--context")
    parser.add_argument("--worst", type=int, default=15, help="largest differences to list (default 15)")
    args = parser.parse_args()

    from db.models import Player, StatsShadowDiff
    from db.models.base import Session
    from utils import stats_shadow

    session = Session()
    try:
        query = session.query(StatsShadowDiff)
        if args.consumer:
            query = query.filter(StatsShadowDiff.consumer == args.consumer)
        if args.context:
            query = query.filter(StatsShadowDiff.context == args.context)
        rows = query.all()
        print("switches: " + ", ".join(f"{c}={stats_shadow.source(c)}" for c in stats_shadow.CONSUMERS)
              + "  (as this shell sees them; services read their own environment)")
        if not rows:
            print("no shadow rows yet")
            return 0
        summary = summarise(rows, lambda consumer: stats_shadow.TOLERANCE.get(consumer, 0.0))
        print(f"{'consumer':<12}{'context':<18}{'pairs':>7}{'compared':>9}{'match':>8}{'rate':>8}"
              f"{'we lack':>9}{'late start':>11}{'wom lacks':>10}{'players':>9}  last seen")
        for (consumer, context), b in sorted(summary.items()):
            rate = f"{100.0 * b['matched'] / b['compared']:.1f}%" if b["compared"] else "-"
            print(f"{consumer:<12}{context:<18}{b['rows']:>7}{b['compared']:>9}{b['matched']:>8}{rate:>8}"
                  f"{b['no_data_ours']:>9}{b['partial']:>11}{b['no_data_wom']:>10}{len(b['players']):>9}"
                  f"  {b['last_seen']}")
        print("(late start = our first reading in the window came more than an hour after it "
              "opened; left out of the match rate)")

        differing = [r for r in rows if r.our_value is not None and r.wom_value is not None
                     and not is_partial(r) and abs(r.our_value - r.wom_value) > stats_shadow.TOLERANCE.get(r.consumer, 0.0)]
        differing.sort(key=lambda r: abs(r.our_value - r.wom_value), reverse=True)
        if differing and args.worst > 0:
            names = dict(session.query(Player.player_id, Player.player_name).filter(
                Player.player_id.in_({r.player_id for r in differing[:args.worst] if r.player_id})))
            print(f"\nlargest differences ({min(args.worst, len(differing))} of {len(differing)}):")
            for r in differing[:args.worst]:
                who = names.get(r.player_id, "-") if r.player_id else "-"
                print(f"  {r.consumer} {r.context} {who} {r.metric}: wom {r.wom_value:g} ours {r.our_value:g} "
                      f"(delta {r.our_value - r.wom_value:+g}, source {r.our_source}, "
                      f"seen {r.observations}x) {r.detail or ''}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
