"""
Restore Royal Titans staff-crown drops falsely rejected by the wiki check.

Between 2026-07-16 and 2026-07-26 the high-value (>1M GP) drop verification ran
a stale pre-fix copy of osrs_api/semantic.py (utils/osrs_api/, removed the same
day this script was added), which rejected every "Fire/Ice element staff crown
from Royal Titans" submission. The drop rows were never written, but the same
crowns arrived as collection-log submissions, which were accepted — so the
`collection` table is the authoritative victim list (player, item, npc 13980,
timestamp, screenshot).

This script re-files one drop per crown clog entry in the window:
  - value: live GE price via get_true_item_value (unit value, quantity 1)
  - authed/used_api = 1, partition from the clog date, image_url carried over
  - unique_id = "restore-crowns-c{log_id}" (synthetic; originals expired with
    the 24h Redis submission-status TTL)
Idempotent: skips any clog entry that already has a drop row for the same
player+item within +/-6h (covers both re-runs and organically re-submitted
drops). After inserting, rebuilds each affected player's Redis leaderboards
via redis_updates.force_update_player (authoritative, DB-driven).

    python -m scripts.restore_rejected_crown_drops            # dry-run
    python -m scripts.restore_rejected_crown_drops --apply    # write
"""

import argparse
import asyncio
import sys
from datetime import timedelta

sys.path.insert(0, ".")

from sqlalchemy import text

from db import Session
from db.models.drop import Drop

CROWN_ITEM_IDS = (30628, 30629, 30631, 30632)
ROYAL_TITANS_NPC_ID = 13980
WINDOW_START = "2026-07-16 00:00:00"
WINDOW_END = "2026-07-26 10:21:00"  # fixed code went live (consumer restart)


def fetch_crown_values(session, items: set) -> dict:
    """Live GE unit values keyed by item_id, falling back to each item's most
    recent drop value. One event loop for all lookups — utils.ge_value caches
    an aiohttp session bound to the loop that created it."""
    from utils.ge_value import get_true_item_value

    fallbacks = {}
    for item_id, item_name in items:
        fallbacks[item_id] = session.execute(
            text(
                "SELECT value FROM drops WHERE item_id = :iid AND value > 0 "
                "ORDER BY drop_id DESC LIMIT 1"
            ),
            {"iid": item_id},
        ).scalar() or 0

    async def _fetch_all():
        return {
            item_id: int(await get_true_item_value(
                item_name, int(fallbacks[item_id]), item_id=item_id))
            for item_id, item_name in items
        }

    return asyncio.run(_fetch_all())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write drop rows and rebuild Redis (default: dry-run)")
    args = parser.parse_args()

    session = Session()
    try:
        clogs = session.execute(
            text(
                "SELECT c.log_id, c.item_id, i.item_name, c.player_id, p.player_name, "
                "       c.date_added, c.image_url, c.used_api "
                "FROM collection c "
                "JOIN items i ON i.item_id = c.item_id "
                "LEFT JOIN players p ON p.player_id = c.player_id "
                "WHERE c.item_id IN :item_ids AND c.npc_id = :npc_id "
                "  AND c.date_added >= :start AND c.date_added < :end "
                "ORDER BY c.date_added"
            ),
            {
                "item_ids": CROWN_ITEM_IDS,
                "npc_id": ROYAL_TITANS_NPC_ID,
                "start": WINDOW_START,
                "end": WINDOW_END,
            },
        ).fetchall()
        print(f"{len(clogs)} crown collection-log entries in the rejection window\n")

        item_pairs = {(c.item_id, c.item_name) for c in clogs}
        values = fetch_crown_values(session, item_pairs)
        for item_id, item_name in sorted(item_pairs):
            print(f"unit value for {item_name} ({item_id}): {values[item_id]:,} gp")
        print()

        to_insert, skipped = [], 0
        for c in clogs:
            existing = session.execute(
                text(
                    "SELECT drop_id FROM drops "
                    "WHERE player_id = :pid AND item_id = :iid "
                    "  AND date_added BETWEEN :lo AND :hi LIMIT 1"
                ),
                {
                    "pid": c.player_id,
                    "iid": c.item_id,
                    "lo": c.date_added - timedelta(hours=6),
                    "hi": c.date_added + timedelta(hours=6),
                },
            ).scalar()
            if existing:
                print(f"SKIP  {c.player_name or c.player_id}: {c.item_name} "
                      f"@ {c.date_added} — drop {existing} already exists")
                skipped += 1
                continue
            to_insert.append(c)
            print(f"WOULD-RESTORE  {c.player_name or c.player_id}: {c.item_name} "
                  f"@ {c.date_added}  value={values[c.item_id]:,}")

        print(f"\n{len(to_insert)} to restore, {skipped} skipped")

        if not args.apply:
            print("\nDRY-RUN — nothing written. Re-run with --apply to restore.")
            return

        for c in to_insert:
            session.add(Drop(
                item_id=c.item_id,
                player_id=c.player_id,
                npc_id=ROYAL_TITANS_NPC_ID,
                date_added=c.date_added,
                date_updated=c.date_added,
                value=values[c.item_id],
                quantity=1,
                image_url=c.image_url,
                authed=True,
                used_api=bool(c.used_api),
                partition=c.date_added.year * 100 + c.date_added.month,
                unique_id=f"restore-crowns-c{c.log_id}",
            ))
        session.commit()
        print(f"inserted {len(to_insert)} drop rows")

        from services import redis_updates

        for pid in sorted({c.player_id for c in to_insert}):
            ok = redis_updates.force_update_player(pid)
            print(f"force_update_player({pid}) -> {ok}")
        print("done")
    finally:
        session.close()


if __name__ == "__main__":
    main()
