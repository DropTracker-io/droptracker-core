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

The first two are unambiguous. The third is not: for most tradeable override
items the client value lands within a few percent of the override answer, and
rewriting those at today's component prices would be churn — some of it
downward, on values that are already right. So only client values that are
*materially* off are repaired (``_damage_reason``); in the 2026-08-28 window
that separates cleanly, near misses at 1.4-3.4% and real damage at 25-95%.

Re-running is idempotent: a repaired row is then within tolerance and is left
alone on the next pass.

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


def _fallback_values() -> dict:
    """item_id → the override's flat fallback_value (the outage's other floor)."""
    from utils import value_overrides

    return {
        int(o["item_id"]): int(o.get("fallback_value") or 0)
        for o in value_overrides.all_active()
        if o.get("item_id") is not None
    }


def _damage_reason(stored: int, computed: int, fallback: int, tolerance: float):
    """Why this drop's value is the outage's and not the override's — or None.

    A *tradeable* override item degraded to the client-reported value, which
    is usually close to the override answer and sometimes wildly under it
    (a Bludgeon spine's own GE price is ~1.8M; the override says 1/3 of an
    assembled bludgeon, ~6.0M). Only the wild ones are damage worth repairing:
    rewriting a value that is already within a few percent would be churn, and
    at today's component prices some of it would move a correct value *down*.

    The measured gap in the 2026-08-28 window is clean — near misses ran
    1.4–3.4% off and real damage 25–95% — so the default tolerance sits in the
    middle of it rather than on either population.
    """
    if stored == 0:
        return "zero"
    if fallback and stored == fallback:
        return "flat fallback"
    if computed and abs(stored - computed) / computed > tolerance:
        return "client value"
    return None


async def _recompute_unit_values(item_ids, session):
    """item_id → freshly computed unit value, via the real intake valuation.

    Returns ``None`` for an item whose value still can't be resolved, so the
    caller can refuse to write rather than re-storing the outage's zeros.
    """
    from db import ItemList
    from utils.ge_value import close_aiohttp_sessions, get_true_item_value

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
    # The loop ends with this script's event loop, so hand the pooled
    # connections back rather than leaving aiohttp to complain about them.
    await close_aiohttp_sessions()
    return names, values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=DEFAULT_SINCE, help="window start (UTC)")
    parser.add_argument("--commit", action="store_true", help="write (default: dry run)")
    parser.add_argument(
        "--drift-tolerance", type=float, default=0.10,
        help="how far a client-reported value may sit from the override before "
             "it counts as damage (default 0.10; see _damage_reason)",
    )
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

        fallbacks = _fallback_values()
        changed, near_misses = [], []
        for r in rows:
            reason = _damage_reason(
                int(r.value), values[int(r.item_id)],
                fallbacks.get(int(r.item_id), 0), args.drift_tolerance,
            )
            (changed if reason else near_misses).append(r)

        print(f"Window:   {args.since} → now")
        print(f"Override-item drops in window: {len(rows)}")
        print(f"Damaged:  {len(changed)} drops "
              f"({len({r.player_id for r in changed})} players)")
        print(f"Left alone: {len(near_misses)} drops whose client-reported value is "
              f"within {args.drift_tolerance:.0%} of the override\n")

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
