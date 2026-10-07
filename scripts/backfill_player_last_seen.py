"""
Seed ``player_last_seen`` (web132a) from what we already know.

The live writer (utils/player_last_seen.py) only knows about plugin activity
from the moment it is deployed. This fills in everyone before that from:

* ``player_plugin_versions.last_seen``  every submission since 2026-10-04,
                                        to within six hours;
* ``player_plugin_config.captured_at``  the last settings snapshot (6.0.16+);
* the player's newest plugin drop       ``drops`` via the
                                        (player_id, date_added) index, newest
                                        first, skipping manual website
                                        submissions. One indexed seek per
                                        player, never an aggregate.

``player_state.last_synced_at`` is not copied: the data API already takes
the newer of it and this table at read time.

Writes use GREATEST, so a run after the live writer is deployed never moves
anyone backwards, and a re-run is harmless.

    python -m scripts.backfill_player_last_seen               # dry run: counts + sample
    python -m scripts.backfill_player_last_seen --apply
    python -m scripts.backfill_player_last_seen --apply --no-drops   # cheap sources only
"""

import argparse
import sys
import time

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session

BATCH = 500
#: Per-statement server ceiling (MariaDB). One seek should take milliseconds.
STATEMENT_SECONDS = 10

_UPSERT = text(
    "INSERT INTO player_last_seen (player_id, last_seen_at) VALUES (:pid, :seen) "
    "ON DUPLICATE KEY UPDATE "
    "last_seen_at = GREATEST(player_last_seen.last_seen_at, VALUES(last_seen_at))"
)


def _real(value):
    """A DATETIME, or None for NULL and MySQL's zero-date (returned as str)."""
    return value if value is not None and hasattr(value, "timetuple") else None


def _cheap_sources(session, player_ids):
    found = {}

    def fold(pid, seen):
        seen = _real(seen)
        if seen is not None and (found.get(pid) is None or seen > found[pid]):
            found[pid] = seen

    ids = tuple(player_ids)
    for pid, seen in session.execute(text(
        f"SET STATEMENT max_statement_time={STATEMENT_SECONDS} FOR "
        "SELECT player_id, MAX(last_seen) FROM player_plugin_versions "
        "WHERE player_id IN :ids GROUP BY player_id"
    ).bindparams(ids=ids)):
        fold(int(pid), seen)
    for pid, seen in session.execute(text(
        f"SET STATEMENT max_statement_time={STATEMENT_SECONDS} FOR "
        "SELECT player_id, captured_at FROM player_plugin_config "
        "WHERE player_id IN :ids"
    ).bindparams(ids=ids)):
        fold(int(pid), seen)
    return found


def _newest_plugin_drop(session, player_id):
    # ORDER BY ... LIMIT 1 walks the composite index backwards and stops at
    # the first non-manual row; MAX() with the source filter would read every
    # drop the player has.
    return _real(session.execute(text(
        f"SET STATEMENT max_statement_time={STATEMENT_SECONDS} FOR "
        "SELECT date_added FROM drops FORCE INDEX (ix_drops_player_id_date_added) "
        "WHERE player_id = :pid AND (source IS NULL OR source <> 'manual') "
        "ORDER BY date_added DESC LIMIT 1"
    ).bindparams(pid=player_id)).scalar())


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--no-drops", action="store_true",
                        help="skip the per-player drops seek")
    parser.add_argument("--pace", type=float, default=0.2,
                        help="seconds to sleep between batches (default 0.2)")
    args = parser.parse_args()

    session = Session()
    try:
        player_ids = [int(r[0]) for r in session.execute(text(
            "SELECT player_id FROM players ORDER BY player_id"))]
        print(f"{len(player_ids)} players; drops seek {'off' if args.no_drops else 'on'}; "
              f"{'APPLY' if args.apply else 'dry run'}")

        seeded, empty, samples = 0, 0, []
        started = time.monotonic()
        for offset in range(0, len(player_ids), BATCH):
            batch = player_ids[offset:offset + BATCH]
            found = _cheap_sources(session, batch)
            if not args.no_drops:
                for pid in batch:
                    seen = _newest_plugin_drop(session, pid)
                    if seen is not None and (found.get(pid) is None or seen > found[pid]):
                        found[pid] = seen
            empty += len(batch) - len(found)
            seeded += len(found)
            if len(samples) < 5:
                samples.extend(list(found.items())[:5 - len(samples)])
            if args.apply and found:
                session.execute(_UPSERT, [{"pid": pid, "seen": seen}
                                          for pid, seen in found.items()])
                session.commit()
            else:
                session.rollback()
            done = offset + len(batch)
            print(f"  {done}/{len(player_ids)}  with a timestamp: {seeded}  "
                  f"none: {empty}  ({time.monotonic() - started:.0f}s)", flush=True)
            if args.pace:
                time.sleep(args.pace)

        print(f"done: {seeded} players {'written' if args.apply else 'would be written'}, "
              f"{empty} with no known activity")
        for pid, seen in samples:
            print(f"  e.g. player {pid}: {seen.isoformat()}")
    finally:
        session.rollback()
        session.close()


if __name__ == "__main__":
    main()
