"""Re-value drops that the 2026-08-28 GE price outage valued wrongly.

**The incident.** ``prices.runescape.wiki`` blocklisted the User-Agent that
``utils/ge_value.py`` kept privately (see ``utils/wiki_ua.py``). From
2026-08-28 ~15:00 UTC every GE lookup 403'd, and because the failure was
swallowed, override-priced items were stored at whatever
``get_true_item_value`` fell back to instead of their real worth:

* ``value = 0`` — the common case. An untradeable component (Araxxor parts,
  the DT2 vestiges, bludgeon pieces) reports 0gp client-side, and with the
  override's ``fallback_value`` also 0 that zero is what got stored. No
  notification was sent, and the drop counts for nothing on any board.
* ``value = fallback_value`` — Mokhaiotl cloth landed on a flat 5,000,000
  instead of its computed ~110M.
* ``value = <client-reported>`` — a *tradeable* override item (Araxyte fang)
  silently degraded from the override's rancour-minus-torture math to the
  raw client value, ~32M instead of ~110M.

All three are the same bug and all three are in scope, so this script does not
try to detect a damaged value: it re-values **every** override-item drop in
the window through the real ``get_true_item_value``, which is exactly what
intake would have stored. That is idempotent — re-running it just re-applies
current component prices.

Not in scope: non-override drops. Those fell back to the client-reported
value, which for a tradeable item is the item's real GE price, so they are
approximately right and there are ~670k/day of them. The lost guarantee there
is anti-spoofing, not accuracy, and it is restored going forward by the UA fix.
Notifications are NOT replayed — announcing five-day-old drops would be noise.

Fixes ``drops.value``, re-folds the affected ``player_{item,npc}_hourly_totals``
buckets (targeted cells only — a whole-day re-fold on this table has timed out
before), and rebuilds each affected player's Redis totals.

Usage:
    venv/bin/python -m scripts.backfill_ge_403_zero_values            # dry run
    venv/bin/python -m scripts.backfill_ge_403_zero_values --commit
    venv/bin/python -m scripts.backfill_ge_403_zero_values --since '2026-08-28 15:00:00'
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import bindparam, text  # noqa: E402

# The hour the prices host started refusing us: the last correctly-valued
# override drop is 14:xx and the first zero is 17:xx on 2026-08-28.
DEFAULT_SINCE = "2026-08-28 15:00:00"

# expanding=True: SQLAlchemy's text() does not expand a tuple into an IN list
# on its own, it binds it as one opaque parameter and matches nothing.
_AFFECTED_SQL = text(
    "SELECT drop_id, player_id, item_id, npc_id, value, quantity, date_added "
    "FROM drops "
    "WHERE date_added >= :since AND item_id IN :item_ids "
    "ORDER BY date_added"
).bindparams(bindparam("item_ids", expanding=True))

_UPDATE_SQL = text(
    "UPDATE drops SET value = :value WHERE drop_id IN :ids"
).bindparams(bindparam("ids", expanding=True))

# Targeted re-fold: only the (player, item, hour) buckets this script touched.
# services/item_totals.refold_day recomputes an entire day, which on this table
# has hit lock-wait timeouts — see the buffer-pool incident notes.
_REFOLD_ITEM_CELL_SQL = text(
    "INSERT INTO player_item_hourly_totals "
    "  (player_id, item_id, date_hour, `partition`, quantity, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), :partition, "
    "       SUM(d.quantity), SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.player_id = :player_id AND d.item_id = :item_id "
    "  AND DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') = :date_hour "
    "GROUP BY d.player_id, d.item_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') "
    "ON DUPLICATE KEY UPDATE "
    "  quantity = VALUES(quantity), total_value = VALUES(total_value), "
    "  drop_count = VALUES(drop_count), last_drop_time = VALUES(last_drop_time)"
)

_REFOLD_NPC_CELL_SQL = text(
    "INSERT INTO player_npc_hourly_totals "
    "  (player_id, npc_id, date_hour, `partition`, total_value, drop_count, last_drop_time) "
    "SELECT d.player_id, d.npc_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), :partition, "
    "       SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
    "FROM drops d "
    "WHERE d.player_id = :player_id AND d.npc_id = :npc_id "
    "  AND DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') = :date_hour "
    "GROUP BY d.player_id, d.npc_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H') "
    "ON DUPLICATE KEY UPDATE "
    "  total_value = VALUES(total_value), drop_count = VALUES(drop_count), "
    "  last_drop_time = VALUES(last_drop_time)"
)


def _partition_of(when: datetime) -> int:
    """Rollup partition key: YYYYMM of the drop's own date (see utils/partitions)."""
    return int(when.strftime("%Y%m"))


async def _recompute_unit_values(item_ids, session):
    """item_id → freshly computed unit value, via the real intake valuation.

    Returns ``None`` for an item whose value still can't be resolved, so the
    caller can refuse to write rather than re-storing the outage's zeros.
    """
    from db import ItemList
    from utils.ge_value import get_true_item_value

    names = {
        int(iid): name
        for iid, name in session.query(ItemList.item_id, ItemList.item_name)
        .filter(ItemList.item_id.in_(sorted(item_ids)))
        .all()
    }

    values = {}
    for item_id in sorted(item_ids):
        name = names.get(item_id)
        if not name:
            values[item_id] = None
            continue
        # provided_value=0 on purpose: we want the override/GE answer, never a
        # client-reported number laundered back into the DB.
        computed = int(await get_true_item_value(name, 0, item_id=item_id))
        values[item_id] = computed or None
    return names, values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=DEFAULT_SINCE, help="window start (UTC)")
    parser.add_argument("--commit", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()

    from db import Session
    from utils import value_overrides

    item_ids = value_overrides.active_item_ids()
    if not item_ids:
        print("No active item_value_overrides — nothing to re-value.")
        return 1

    session = Session()
    try:
        rows = session.execute(
            _AFFECTED_SQL, {"since": args.since, "item_ids": list(item_ids)}
        ).fetchall()
        if not rows:
            print(f"No override-item drops since {args.since}.")
            return 0

        names, values = asyncio.run(
            _recompute_unit_values({int(r.item_id) for r in rows}, session)
        )

        unresolved = sorted(i for i, v in values.items() if not v)
        if unresolved:
            # The whole incident was a valuation path that failed quietly. If
            # it is still failing, stop — do not launder another round of
            # zeros into the drops table.
            print("REFUSING TO WRITE — these items still value to 0:")
            for item_id in unresolved:
                print(f"  {item_id} ({names.get(item_id, '?')})")
            print("\nThe GE price API is still unreachable. Check:")
            print("  curl -A \"$(venv/bin/python -c "
                  "'from utils.wiki_ua import USER_AGENT; print(USER_AGENT)')\" \\")
            print("    'https://prices.runescape.wiki/api/v1/osrs/latest?id=29796'")
            return 1

        changed = [r for r in rows if int(r.value) != values[int(r.item_id)]]

        print(f"Window:   {args.since} → now")
        print(f"Override-item drops in window: {len(rows)}")
        print(f"Would change: {len(changed)} drops "
              f"({len({r.player_id for r in changed})} players)\n")

        by_item = defaultdict(list)
        for r in changed:
            by_item[int(r.item_id)].append(r)
        for item_id, item_rows in sorted(by_item.items(), key=lambda kv: -len(kv[1])):
            olds = sorted({int(r.value) for r in item_rows})
            shown = ", ".join(f"{v:,}" for v in olds[:4]) + ("…" if len(olds) > 4 else "")
            print(f"  {names[item_id]:<24} ({item_id})  {len(item_rows):>3} drops  "
                  f"[{shown}] → {values[item_id]:,}")

        recovered = sum(
            (values[int(r.item_id)] - int(r.value)) * int(r.quantity or 1) for r in changed
        )
        print(f"\nTotal value restored: {recovered:,} gp")

        if not args.commit:
            print("\nDRY RUN — re-run with --commit to apply.")
            return 0

        # 1. drops.value — the authoritative column every board reads.
        for item_id, item_rows in by_item.items():
            session.execute(
                _UPDATE_SQL,
                {"value": values[item_id], "ids": [int(r.drop_id) for r in item_rows]},
            )
        session.commit()
        print(f"\nUpdated {len(changed)} drops.")

        # 2. Hourly rollups — only the buckets those drops sit in.
        item_cells = {
            (int(r.player_id), int(r.item_id), r.date_added.strftime("%Y-%m-%d-%H"),
             _partition_of(r.date_added))
            for r in changed
        }
        npc_cells = {
            (int(r.player_id), int(r.npc_id), r.date_added.strftime("%Y-%m-%d-%H"),
             _partition_of(r.date_added))
            for r in changed if r.npc_id is not None
        }
        for player_id, item_id, date_hour, partition in item_cells:
            session.execute(_REFOLD_ITEM_CELL_SQL, {
                "player_id": player_id, "item_id": item_id,
                "date_hour": date_hour, "partition": partition,
            })
        for player_id, npc_id, date_hour, partition in npc_cells:
            session.execute(_REFOLD_NPC_CELL_SQL, {
                "player_id": player_id, "npc_id": npc_id,
                "date_hour": date_hour, "partition": partition,
            })
        session.commit()
        print(f"Re-folded {len(item_cells)} item and {len(npc_cells)} npc rollup buckets.")

        # 3. Redis. Lootboard images read drops straight from MySQL and
        # self-heal on the next 2-minute cycle, but the website leaderboards
        # are served from Redis and need an explicit rebuild.
        from services.redis_updates import force_update_player

        players = sorted({int(r.player_id) for r in changed})
        rebuilt = 0
        for player_id in players:
            try:
                if force_update_player(player_id, session):
                    rebuilt += 1
            except Exception as exc:  # one bad player must not abort the rest
                print(f"  ! force_update_player({player_id}) failed: {exc}")
        print(f"Rebuilt Redis totals for {rebuilt}/{len(players)} players.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
