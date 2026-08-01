"""Generate monthly / annual recap snapshots (ops + backfill entry point).

Computes recap cards and writes them to ``recap_snapshots``. Dry run by default,
``--apply`` to write, and idempotent — the unique constraint on
``(scope, subject_id, period)`` means re-running a period refreshes rows rather
than duplicating them.

Rendering and Discord delivery are separate concerns; this only produces the
data the card is built from.

One thing does reach outside: EHB is fetched from Wise Old Man and cached in
``recap_wom_gains`` before the cards are computed. That happens on a dry run
too — it caches immutable facts about a closed month, which is exactly what a
dry run needs in place to show what the cards would say. ``--skip-ehb`` opts
out.

Usage
-----
    # what would last month's group recaps look like? (writes nothing)
    python -m scripts.generate_recaps --period 2026-06

    # actually store them
    python -m scripts.generate_recaps --period 2026-06 --apply

    # one subject, printed in full — the fastest way to eyeball the payload
    python -m scripts.generate_recaps --period 2026-06 --group 12 --show
    python -m scripts.generate_recaps --period 2026-06 --player 7137 --show

    # players as well as groups
    python -m scripts.generate_recaps --period 2026-06 --scope player --apply

    # the annual fold (needs the twelve monthly rows to exist already)
    python -m scripts.generate_recaps --period 2025 --apply

Note that 2025 is the earliest complete calendar year: tracked data starts
2024-10-15, so an annual recap for 2024 would cover a partial year.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db import Session  # noqa: E402
from db.models.recap import SCOPE_GROUP, SCOPE_PLAYER  # noqa: E402
from services.recap import (  # noqa: E402
    RosterTooLarge,
    compute_group_month,
    compute_player_month,
    compute_year,
    is_month_period,
    is_year_period,
    period_closed,
    period_partition,
    save_snapshot,
)
from services.recap_ehb import harvest_month_ehb  # noqa: E402
from utils.wiseoldman import close_client  # noqa: E402


def _group_ids(session) -> list[int]:
    """Every group with members. Group 1 is the config template and group 2 is
    the global pseudo-group holding every tracked player; neither is a clan, so
    neither gets a recap."""
    rows = session.execute(
        text(
            "SELECT DISTINCT group_id FROM user_group_association "
            "WHERE group_id NOT IN (1, 2) ORDER BY group_id"
        )
    ).fetchall()
    return [int(r[0]) for r in rows]


def _active_player_ids(session, period: str) -> list[int]:
    """Players with rollup activity in the period.

    Selecting from the rollup rather than ``drops`` keeps this to one ranged
    index read instead of a scan of a 175M-row table.
    """
    partition = period_partition(period)
    year, month = partition // 100, partition % 100
    lo, hi = f"{year:04d}-{month:02d}-01-00", f"{year:04d}-{month:02d}-31-23"
    rows = session.execute(
        text(
            "SELECT DISTINCT player_id FROM player_item_hourly_totals "
            "WHERE date_hour BETWEEN :lo AND :hi"
        ),
        {"lo": lo, "hi": hi},
    ).fetchall()
    return [int(r[0]) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate recap snapshots.")
    ap.add_argument("--period", required=True,
                    help="'YYYY-MM' for a month, 'YYYY' for the annual fold")
    ap.add_argument("--apply", action="store_true",
                    help="write snapshots (default is a dry run)")
    ap.add_argument("--scope", choices=["group", "player", "both"], default="group",
                    help="which subjects to generate (default: group)")
    ap.add_argument("--group", type=int, metavar="ID",
                    help="restrict to one group")
    ap.add_argument("--player", type=int, metavar="ID",
                    help="restrict to one player")
    ap.add_argument("--show", action="store_true",
                    help="pretty-print each computed payload")
    ap.add_argument("--limit", type=int,
                    help="stop after N subjects (smoke tests)")
    ap.add_argument("--force-open", action="store_true",
                    help="generate even though the period hasn't closed yet; the "
                         "numbers will keep moving, so never publish these")
    ap.add_argument("--skip-ehb", action="store_true",
                    help="don't harvest EHB from Wise Old Man first; cards for "
                         "subjects not already harvested will omit the stat")
    args = ap.parse_args()

    period = args.period.strip()
    if not (is_month_period(period) or is_year_period(period)):
        print(f"error: --period must be 'YYYY-MM' or 'YYYY', got {period!r}")
        return 2

    if not period_closed(period) and not args.force_open:
        print(f"error: {period} hasn't closed yet. A partial period is the one "
              f"mistake a recap can't walk back — pass --force-open to preview it.")
        return 2

    session = Session()
    written = skipped = 0
    started = time.time()
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] recap generation for {period}")

    try:
        scopes = (
            [SCOPE_GROUP] if args.scope == "group"
            else [SCOPE_PLAYER] if args.scope == "player"
            else [SCOPE_GROUP, SCOPE_PLAYER]
        )
        if args.group:
            scopes, subjects_by_scope = [SCOPE_GROUP], {SCOPE_GROUP: [args.group]}
        elif args.player:
            scopes, subjects_by_scope = [SCOPE_PLAYER], {SCOPE_PLAYER: [args.player]}
        else:
            subjects_by_scope = {}
            for scope in scopes:
                if is_year_period(period):
                    # The annual fold only makes sense for subjects that already
                    # have monthly rows — anything else would fold nothing.
                    rows = session.execute(
                        text(
                            "SELECT DISTINCT subject_id FROM recap_snapshots "
                            "WHERE scope = :scope AND period LIKE :like"
                        ),
                        {"scope": scope, "like": f"{period}-%"},
                    ).fetchall()
                    subjects_by_scope[scope] = [int(r[0]) for r in rows]
                elif scope == SCOPE_GROUP:
                    subjects_by_scope[scope] = _group_ids(session)
                else:
                    subjects_by_scope[scope] = _active_player_ids(session, period)

        if args.limit:
            for scope in scopes:
                subjects_by_scope[scope] = subjects_by_scope.get(scope, [])[: args.limit]

        # EHB comes from Wise Old Man and is cached per month in
        # `recap_wom_gains`, so it is fetched before any card is computed. This
        # runs on a dry run too, and writes those cache rows: it fills a cache
        # of immutable facts about a closed month, which is the thing a dry run
        # needs in place to show what the cards would say. The annual fold reads
        # the monthly snapshots and needs no harvest of its own.
        if not is_year_period(period) and not args.skip_ehb:
            print(f"  harvesting EHB for {period} from Wise Old Man...")

            async def _harvest():
                try:
                    await harvest_month_ehb(
                        session,
                        period,
                        group_ids=subjects_by_scope.get(SCOPE_GROUP, []),
                        player_ids=subjects_by_scope.get(SCOPE_PLAYER, []),
                        log=print,
                    )
                finally:
                    # One-shot process: hand the HTTP session back, or aiohttp
                    # complains over the report this script exists to print.
                    await close_client()

            asyncio.run(_harvest())

        for scope in scopes:
            subjects = subjects_by_scope.get(scope, [])
            print(f"  {scope}: {len(subjects)} subject(s)")

            for subject_id in subjects:
                try:
                    if is_year_period(period):
                        payload = compute_year(session, scope, subject_id, int(period))
                    elif scope == SCOPE_GROUP:
                        payload = compute_group_month(session, subject_id, period)
                    else:
                        payload = compute_player_month(session, subject_id, period)
                except RosterTooLarge as err:
                    print(f"    skip {scope} {subject_id}: {err}")
                    skipped += 1
                    continue

                if not payload:
                    skipped += 1
                    continue

                name = (payload.get("subject") or {}).get("name") or subject_id
                totals = payload.get("totals", {})
                ehb = (f" ehb={totals['ehb']:,.1f}"
                       if totals.get("ehb") is not None else "")
                print(
                    f"    {scope} {subject_id} ({name}): "
                    f"loot={totals.get('loot', 0):,} drops={totals.get('drops', 0):,}"
                    f"{ehb} rank={(payload.get('rank') or {}).get('position')}"
                )
                if args.show:
                    print(json.dumps(payload, indent=2, default=str))

                if args.apply:
                    save_snapshot(session, scope, subject_id, period, payload)
                    session.commit()
                written += 1

        elapsed = time.time() - started
        verb = "wrote" if args.apply else "would write"
        print(f"[{mode}] {verb} {written} snapshot(s), skipped {skipped} "
              f"(below activity floor or no data) in {elapsed:.1f}s")
        if not args.apply and written:
            print("Re-run with --apply to store them.")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
