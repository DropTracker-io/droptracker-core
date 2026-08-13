"""Build a group's Clan Log from the beginning (ops entry point).

Reads every catalog item this group's members have ever obtained and writes the
``clan_log_firsts`` ledger, then stores the all-time / year / month boards.
Dry run by default; idempotent, because the ledger upserts on
``(group, item, month)`` and a first claim only ever moves earlier.

Cheap enough to run in the open: the catalog is ~300 rare items, so a
500-member clan's entire history is a couple of thousand rows, not the millions
a roster-wide scan would read. Left as a manual per-group step anyway — a very
large, very old group is the one case that takes minutes, and nobody should
discover that by enabling the feature for everyone at once.

Usage
-----
    python -m scripts.clan_log.backfill_group --group 190
    python -m scripts.clan_log.backfill_group --group 190 --apply
    python -m scripts.clan_log.backfill_group --all --min-members 25 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import text  # noqa: E402

from db.models.base import Session  # noqa: E402
from services.clan_log import (  # noqa: E402
    PERIOD_ALL,
    RosterTooLarge,
    load_board,
    load_catalog,
    refresh_group,
)


def _candidate_groups(session, min_members: int) -> list[tuple[int, str, int]]:
    rows = session.execute(
        text(
            "SELECT g.group_id, g.group_name, COUNT(a.player_id) AS members "
            "FROM groups g JOIN user_group_association a ON a.group_id = g.group_id "
            "WHERE g.group_id NOT IN (1, 2) "
            "GROUP BY g.group_id HAVING members >= :floor "
            "ORDER BY members DESC"
        ),
        {"floor": min_members},
    ).fetchall()
    return [(int(r[0]), r[1], int(r[2])) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill a group's Clan Log")
    parser.add_argument("--group", type=int, action="append", default=[],
                        help="group id (repeatable)")
    parser.add_argument("--all", action="store_true", help="every eligible group")
    parser.add_argument("--min-members", type=int, default=10,
                        help="skip groups smaller than this with --all")
    parser.add_argument("--limit", type=int, help="stop after N groups")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()

    if not args.group and not args.all:
        parser.error("pass --group ID or --all")

    session = Session()
    try:
        catalog = load_catalog(session)
        total_items = sum(len(s["items"]) for s in catalog["sections"])
        print(f"catalog {catalog['version']}: {len(catalog['sections'])} sections / "
              f"{total_items} items")

        if args.all:
            targets = _candidate_groups(session, args.min_members)
        else:
            targets = [(gid, "", 0) for gid in args.group]
        if args.limit:
            targets = targets[: args.limit]
        print(f"{len(targets)} group(s) to process\n")

        for group_id, name, members in targets:
            started = time.time()
            try:
                stats = refresh_group(session, group_id, catalog=catalog, full=True)
            except RosterTooLarge as exc:
                print(f"  group {group_id}: SKIPPED — {exc}")
                session.rollback()
                continue
            except Exception as exc:  # one bad group must not stop the run
                print(f"  group {group_id}: FAILED — {exc}")
                session.rollback()
                continue

            if args.apply:
                session.commit()
                board = load_board(session, group_id, PERIOD_ALL) or {}
            else:
                board = None
                session.flush()

            summary = ""
            if board:
                s = board.get("summary", {})
                summary = f" -> {s.get('obtained')}/{s.get('total')} slots ({s.get('pct')}%)"
            label = f"{name} " if name else ""
            print(f"  group {group_id} {label}({members} members): {stats}"
                  f"{summary}  [{time.time() - started:.1f}s]")

            if not args.apply:
                session.rollback()

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
