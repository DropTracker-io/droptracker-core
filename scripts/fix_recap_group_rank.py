"""Rewrite the ``rank`` block on stored group recap cards.

``services/recap._group_rank`` used a bare ``zrevrank`` over
``gleaderboard:{partition}``, which carries group 2 ("DropTracker.io") — the
global group holding every tracked player. Its score is an order of magnitude
above any real clan, so it sat at the top permanently and pushed every clan
down exactly one place: for 2026-08 the top clan's own card read "#2 of 284"
while the site's leaderboard called it #1.

The rank is the only wrong field, so this rewrites just that block rather than
recomputing whole cards — the totals are unchanged and re-deriving them would
be a slower way to reach the same payload.

Dry run by default; ``--apply`` writes.

    python -m scripts.fix_recap_group_rank --period 2026-08
    python -m scripts.fix_recap_group_rank --period 2026-08 --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Session  # noqa: E402
from db.models.recap import RecapSnapshot, SCOPE_GROUP  # noqa: E402
from services.recap import _group_rank, period_partition, save_snapshot  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--period", required=True, help="'YYYY-MM'")
    ap.add_argument("--apply", action="store_true", help="actually write")
    args = ap.parse_args()

    period = args.period.strip()
    partition = period_partition(period)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] group recap ranks for {period} (partition {partition})")

    session = Session()
    changed = unchanged = norank = 0
    try:
        rows = (
            session.query(RecapSnapshot)
            .filter(RecapSnapshot.scope == SCOPE_GROUP, RecapSnapshot.period == period)
            .all()
        )
        print(f"  {len(rows)} stored card(s)")

        for row in rows:
            try:
                payload = json.loads(row.payload)
            except (TypeError, ValueError):
                print(f"  group {row.subject_id}: unreadable payload, skipped")
                continue

            rank, of, board_loot = _group_rank(row.subject_id, partition)
            if rank is None:
                norank += 1
                continue

            before = payload.get("rank") or {}
            after = dict(before)
            after["position"] = rank
            after["of"] = of
            after["board_loot"] = board_loot
            if after == before:
                unchanged += 1
                continue

            changed += 1
            if changed <= 10:
                print(
                    f"  group {row.subject_id}: "
                    f"#{before.get('position')} of {before.get('of')} "
                    f"-> #{rank} of {of}"
                )
            if args.apply:
                payload["rank"] = after
                save_snapshot(session, SCOPE_GROUP, row.subject_id, period, payload)

        if args.apply:
            session.commit()
    finally:
        session.close()

    if changed > 10:
        print(f"  ... and {changed - 10} more")
    print(
        f"[{mode}] corrected={changed} already-correct={unchanged} "
        f"no-rank-on-board={norank}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
