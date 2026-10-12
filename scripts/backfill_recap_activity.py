"""Re-weight the activity histograms on stored recap snapshots.

Until 2026-10-12 a card's ``activity.by_hour`` / ``by_weekday`` summed GP, so
"Prime time" was the hour the month's biggest drop landed — a single 4.5M clue
at 04:25 UTC outvoted every evening of play (bug report #191). They now count
active hours (see ``services.recap.activity_histograms``) and carry
``activity.weight``. This rewrites only that block on every stored
monthly/annual group and player card that lacks the stamp; every other figure
on the card is left exactly as it was generated.

* Month rows recompute the histograms from ``player_item_hourly_totals``. A
  player card covers ``[subject_id]``; a group card covers the clan's *current*
  visible roster, since the roster a card was built from isn't stored — the
  same thing a ``generate_recaps`` re-run would use.
* Year rows re-sum their stored months (as ``compute_year`` does), so run the
  months first — one invocation does both, months before years.
* ``generated_at`` is bumped on each rewritten row: it is the PNG cache key
  (``services/recap_image.py``), so the posted image re-renders on next view.
  Delivery dedupes on its own ledger, so nothing is re-sent.

Usage
-----
    # what would change? (writes nothing)
    python -m scripts.backfill_recap_activity
    python -m scripts.backfill_recap_activity --period 2026-09 --show 20

    python -m scripts.backfill_recap_activity --apply
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Session  # noqa: E402
from db.models.recap import SCOPE_GROUP, SCOPE_PLAYER, RecapSnapshot  # noqa: E402
from services.recap import (  # noqa: E402
    ACTIVITY_WEIGHT,
    MAX_ROSTER_FOR_RECAP,
    _visible_group_player_ids,
    activity_histograms,
    is_month_period,
    is_year_period,
    year_months,
)

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _peak(values) -> str:
    if not values or not any(values):
        return "-"
    i = max(range(len(values)), key=lambda k: values[k])
    return f"{i:02d}:00" if len(values) == 24 else DAYS[i]


def _describe(activity: dict) -> str:
    return f"{_peak(activity.get('by_hour'))} {_peak(activity.get('by_weekday'))}"


def _month_activity(session, row, payload) -> dict | None:
    if row.scope == SCOPE_PLAYER:
        ids = [row.subject_id]
    else:
        ids = _visible_group_player_ids(session, row.subject_id)
        if not ids or len(ids) > MAX_ROSTER_FOR_RECAP:
            return None
    return activity_histograms(session, ids, row.period)


def _year_activity(session, row, months: dict) -> dict | None:
    by_hour, by_weekday, folded = [0] * 24, [0] * 7, 0
    for period in year_months(int(row.period)):
        snap = months.get((row.scope, row.subject_id, period))
        if snap is None:
            continue
        activity = snap.get("activity") or {}
        if activity.get("weight") != ACTIVITY_WEIGHT:
            return None  # a month we couldn't re-weight; don't mix units
        for i, v in enumerate((activity.get("by_hour") or [])[:24]):
            by_hour[i] += int(v or 0)
        for i, v in enumerate((activity.get("by_weekday") or [])[:7]):
            by_weekday[i] += int(v or 0)
        folded += 1
    if not folded:
        return None
    return {"by_hour": by_hour, "by_weekday": by_weekday}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the rows (default: dry run)")
    ap.add_argument("--period", help="only this period (YYYY-MM or YYYY)")
    ap.add_argument("--scope", choices=[SCOPE_GROUP, SCOPE_PLAYER], help="only this scope")
    ap.add_argument("--show", type=int, default=10, help="print the first N changes")
    args = ap.parse_args()

    scopes = [args.scope] if args.scope else [SCOPE_GROUP, SCOPE_PLAYER]
    stats = {"rewritten": 0, "already": 0, "skipped": 0, "peak_hour_moved": 0}
    shown = 0

    with Session() as session:
        q = session.query(RecapSnapshot).filter(RecapSnapshot.scope.in_(scopes))
        if args.period:
            q = q.filter(RecapSnapshot.period == args.period)
        rows = q.all()
        # Months before years: a year re-sums its months' (new) histograms.
        rows.sort(key=lambda r: (is_year_period(r.period), r.period, r.scope, r.subject_id))

        # Every stored month payload, keyed for the year fold — updated in place
        # as months are rewritten so a dry run previews the years correctly too.
        months: dict = {}
        if any(is_year_period(r.period) for r in rows):
            for r in session.query(RecapSnapshot).filter(RecapSnapshot.scope.in_(scopes)):
                if is_month_period(r.period):
                    try:
                        months[(r.scope, r.subject_id, r.period)] = json.loads(r.payload)
                    except (TypeError, ValueError):
                        pass

        for row in rows:
            try:
                payload = json.loads(row.payload)
            except (TypeError, ValueError):
                stats["skipped"] += 1
                continue
            old = payload.get("activity") or {}
            if old.get("weight") == ACTIVITY_WEIGHT:
                stats["already"] += 1
                continue

            if is_month_period(row.period):
                new = _month_activity(session, row, payload)
            elif is_year_period(row.period):
                new = _year_activity(session, row, months)
            else:
                new = None
            if new is None:
                stats["skipped"] += 1
                continue

            new["weight"] = ACTIVITY_WEIGHT
            payload["activity"] = new
            if is_month_period(row.period):
                months[(row.scope, row.subject_id, row.period)] = payload

            if _peak(old.get("by_hour")) != _peak(new["by_hour"]):
                stats["peak_hour_moved"] += 1
            if shown < args.show:
                print(f"  {row.scope:6} {row.subject_id:>8} {row.period:7}  "
                      f"{_describe(old):10} -> {_describe(new)}")
                shown += 1

            if args.apply:
                now = datetime.now()
                payload["generated_at"] = now.isoformat(timespec="seconds")
                row.payload = json.dumps(payload, separators=(",", ":"), default=str)
                row.generated_at = now
            stats["rewritten"] += 1

        if args.apply:
            session.commit()
        else:
            session.rollback()

    verb = "rewrote" if args.apply else "would rewrite"
    print(f"\n{verb} {stats['rewritten']} rows "
          f"(peak hour changed on {stats['peak_hour_moved']}); "
          f"{stats['already']} already active-hours, {stats['skipped']} skipped")
    if not args.apply:
        print("dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
