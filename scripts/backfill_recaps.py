"""Backfill historical monthly group recap snapshots.

``scripts.generate_recaps`` generates one period for every group; this fills
the *history*: for each group, every closed month the group existed for in its
entirety (created before the month began), from the first full month of
tracked data (2024-11 — tracking starts 2024-10-15) through the last closed
month. Existing snapshots are left untouched — this fills gaps, it never
refreshes rows the delivery pipeline (or an earlier backfill) already wrote,
because those were computed nearer the month they describe and the roster
drifts.

Deliberately does NOT harvest EHB from Wise Old Man: a full backfill would be
thousands of bulk-gained calls for a stat no stored card carries yet (EHB
first bakes into the 2026-08 run). Cards omit an unharvested stat rather than
zeroing it, so the backfilled cards match every card already stored.

Same caveat as the group-14 backfill this generalises: the roster is
*today's* ``user_group_association``, so a historical month is measured over
the current membership. Departed members keep their association rows, which
softens the drift.

Usage
-----
    # what would be written? (writes nothing)
    python -m scripts.backfill_recaps

    # actually store the snapshots
    python -m scripts.backfill_recaps --apply

    # one group, or a bounded window
    python -m scripts.backfill_recaps --group 45 --apply
    python -m scripts.backfill_recaps --start 2025-01 --end 2025-12
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db import Session  # noqa: E402
from db.models.recap import SCOPE_GROUP  # noqa: E402
from services.recap import (  # noqa: E402
    RosterTooLarge,
    compute_group_month,
    is_month_period,
    month_period,
    period_closed,
    previous_month_period,
    save_snapshot,
)

# Tracked data starts 2024-10-15, so 2024-11 is the first month a recap can
# cover in full. 2024-10 would silently describe half a month.
FIRST_FULL_DATA_MONTH = "2024-11"

# Where submission screenshots live when `image_url` maps onto the public
# prefix — used to drop references to files the prune timer already deleted.
_STATIC_IMG_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "assets", "img",
)
_PUBLIC_IMG_PREFIX = "https://www.droptracker.io/img/"


def _drop_pruned_image(payload: dict) -> bool:
    """Null out ``biggest_drop.image_url`` when it points at a file the
    prune timer has already deleted.

    The delivery pipeline never faces this: it snapshots a month within days
    of its close and the prune script protects what snapshots reference. A
    backfill references screenshots that were eligible for pruning for months
    before any snapshot protected them — a URL to a deleted file draws a
    broken-image box on the card, which is worse than the blank the layout
    handles. Only locally-served URLs can be checked; foreign hosts pass
    through untouched.
    """
    biggest = payload.get("biggest_drop") or {}
    url = biggest.get("image_url")
    if not url or not url.startswith(_PUBLIC_IMG_PREFIX):
        return False
    local = os.path.join(_STATIC_IMG_ROOT, url[len(_PUBLIC_IMG_PREFIX):])
    if os.path.isfile(local):
        return False
    biggest["image_url"] = None
    return True


def _month_start(period: str) -> datetime:
    year, month = period.split("-")
    return datetime(int(year), int(month), 1)


def _next_period(period: str) -> str:
    year, month = (int(x) for x in period.split("-"))
    return f"{year + 1:04d}-01" if month == 12 else f"{year:04d}-{month + 1:02d}"


def _periods(start: str, end: str) -> list[str]:
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur = _next_period(cur)
    return out


def _group_created(session) -> dict[int, datetime]:
    """``{group_id: created}`` for every real group.

    A NULL ``date_added`` (pre-dates the column's default) falls back to the
    group's earliest config write — later than the true creation, so it can
    only *under*-claim which months the group existed for, never invent one.
    """
    rows = session.execute(
        text(
            "SELECT g.group_id, COALESCE(g.date_added, ("
            "  SELECT MIN(c.updated_at) FROM group_configurations c "
            "  WHERE c.group_id = g.group_id)) "
            "FROM groups g WHERE g.group_id NOT IN (1, 2)"
        )
    ).fetchall()
    created = {}
    for gid, ts in rows:
        if ts is None:
            print(f"  skip group {gid}: no creation evidence at all")
            continue
        created[int(gid)] = ts
    return created


def _existing(session) -> set[tuple[int, str]]:
    rows = session.execute(
        text(
            "SELECT subject_id, period FROM recap_snapshots "
            "WHERE scope = :scope AND LENGTH(period) = 7"
        ),
        {"scope": SCOPE_GROUP},
    ).fetchall()
    return {(int(gid), period) for gid, period in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill historical group recaps.")
    ap.add_argument("--apply", action="store_true",
                    help="write snapshots (default is a dry run)")
    ap.add_argument("--group", type=int, metavar="ID", help="restrict to one group")
    ap.add_argument("--start", default=FIRST_FULL_DATA_MONTH, metavar="YYYY-MM",
                    help=f"earliest month (default {FIRST_FULL_DATA_MONTH})")
    ap.add_argument("--end", metavar="YYYY-MM",
                    help="latest month (default: last closed month)")
    ap.add_argument("--limit", type=int,
                    help="stop after N written snapshots (smoke tests)")
    args = ap.parse_args()

    end = args.end or previous_month_period(month_period(
        datetime.now().year * 100 + datetime.now().month))
    for label, period in (("--start", args.start), ("--end", end)):
        if not is_month_period(period):
            print(f"error: {label} must be 'YYYY-MM', got {period!r}")
            return 2
    if not period_closed(end):
        print(f"error: --end {end} hasn't closed yet")
        return 2

    session = Session()
    mode = "APPLY" if args.apply else "DRY RUN"
    started = time.time()
    written = existing_kept = below_floor = 0
    print(f"[{mode}] recap backfill, {args.start} .. {end}")

    try:
        created = _group_created(session)
        if args.group:
            if args.group not in created:
                print(f"error: group {args.group} not found")
                return 2
            created = {args.group: created[args.group]}
        have = _existing(session)

        for period in _periods(args.start, end):
            eligible = sorted(
                gid for gid, ts in created.items() if ts < _month_start(period)
            )
            todo = [gid for gid in eligible if (gid, period) not in have]
            existing_kept += len(eligible) - len(todo)
            if not todo:
                continue
            print(f"  {period}: {len(todo)} group(s) to fill "
                  f"({len(eligible) - len(todo)} already stored)")

            for gid in todo:
                try:
                    payload = compute_group_month(session, gid, period)
                except RosterTooLarge as err:
                    print(f"    skip group {gid}: {err}")
                    continue
                if not payload:
                    below_floor += 1
                    continue
                _drop_pruned_image(payload)
                name = (payload.get("subject") or {}).get("name") or gid
                totals = payload.get("totals", {})
                print(f"    group {gid} ({name}): loot={totals.get('loot', 0):,} "
                      f"drops={totals.get('drops', 0):,} "
                      f"active={totals.get('members_active', 0)}")
                if args.apply:
                    save_snapshot(session, SCOPE_GROUP, gid, period, payload)
                    session.commit()
                written += 1
                if args.limit and written >= args.limit:
                    raise KeyboardInterrupt

    except KeyboardInterrupt:
        print("  stopped at --limit")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    elapsed = time.time() - started
    verb = "wrote" if args.apply else "would write"
    print(f"[{mode}] {verb} {written} snapshot(s); kept {existing_kept} existing, "
          f"{below_floor} below the activity floor, in {elapsed:.1f}s")
    if not args.apply and written:
        print("Re-run with --apply to store them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
