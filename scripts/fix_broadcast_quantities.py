#!/usr/bin/env python3
"""
Repair clan-chat drops whose quantity was set to the broadcast's coin total.

Why this exists
---------------
Plugin 6.1.30 (hub, 2026-11-24 00:39 UTC) relays clan broadcasts' raw text,
and Jagex's markup since 2026-11-18 reuses ONE variable name, redefined before
each use::

    <int_0=12><int_0:spacer> x Oathplate shards<int_0=1679772> (<int_0:spacer> coins)

``utils.clan_broadcasts.expand_tag_variables`` collected every definition
before substituting, so the quantity read as the coin total: "1679772 x
Oathplate shards", priced at 237B. Fixed in the same commit as this script.

Which rows
----------
Clan-chat drops (``unique_id`` ``cc:%``) above ``--after`` with quantity > 1.
In that window every chat line that printed a quantity also printed a coin
figure, so quantity IS the stack total, and the real count is
``round(quantity / value)`` (value = the server's unit price). An item priced
<= 1 gp (untradeable) gives no ratio and is set to 1. Rows whose ratio is not
within 5% of a whole number are reported and skipped.

Then, like ``scripts/reprice_drops.py``: hourly rollup cells re-folded,
``force_update_player`` per player, per-NPC boards set absolutely. The
current month's ``gleaderboard`` is left to the lootboard generator.
Not touched: the Discord posts already sent (``notified``).

Usage
-----
    ./venv/bin/python -m scripts.fix_broadcast_quantities --after 275177001          # dry run
    ./venv/bin/python -m scripts.fix_broadcast_quantities --after 275177001 --apply

``--apply`` saves the old quantities to ``scripts/data/`` first. Idempotent:
a fixed row's quantity/value ratio is no longer a whole number > 1 of itself.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.reprice_drops import REPO_ROOT, _maint_session  # noqa: E402

from sqlalchemy import bindparam, text  # noqa: E402


def corrected_quantity(quantity: int, value: int):
    """(new_quantity, reason) or (None, reason) when the row should be skipped."""
    if quantity <= 1:
        return None, "already 1"
    if value <= 1:
        return 1, "unpriced item"
    ratio = quantity / value
    new = max(1, round(ratio))
    if abs(ratio - new) > 0.05 * new:
        return None, f"ratio {ratio:.3f} is not a whole count"
    if new == quantity:
        return None, "unchanged"
    return new, "stack total / unit price"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--after", type=int, required=True, help="only drop_id > this")
    ap.add_argument("--until", type=int, help="only drop_id <= this")
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args(argv)

    from scripts.backfill_ge_403_zero_values import (
        _REFOLD_ITEM_CELL_SQL,
        _REFOLD_NPC_CELL_SQL,
        _partition_of,
    )
    from services.redis_updates import RedisLootTracker, force_update_player
    from utils.redis import redis_client

    conn = redis_client.client
    db = _maint_session()
    rows = db.execute(text(
        "SELECT d.drop_id, d.player_id, d.item_id, d.npc_id, d.quantity, d.value, "
        "       d.date_added, p.player_name, i.item_name "
        "FROM drops d JOIN players p ON p.player_id = d.player_id "
        "JOIN items i ON i.item_id = d.item_id "
        "WHERE d.drop_id > :after AND d.drop_id <= :until "
        "  AND d.unique_id LIKE 'cc:%' AND d.quantity > 1 ORDER BY d.drop_id"
    ), {"after": args.after, "until": args.until or 2**62}).fetchall()

    plan, skipped = [], []
    for r in rows:
        new, why = corrected_quantity(int(r.quantity), int(r.value))
        (plan if new is not None else skipped).append((r, new, why))

    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(rows)} chat drops with quantity > 1, "
          f"{len(plan)} to fix, {len(skipped)} skipped\n")
    old_total = new_total = 0
    for r, new, why in plan:
        old_gp, new_gp = int(r.value) * int(r.quantity), int(r.value) * new
        old_total += old_gp
        new_total += new_gp
        print(f"  {r.drop_id} {r.date_added:%m-%d %H:%M} {r.player_name:<13} {r.item_name:<20} "
              f"x{int(r.quantity):,} -> x{new:,}  ({old_gp:,} -> {new_gp:,} gp; {why})")
    for r, _new, why in skipped:
        print(f"  SKIP {r.drop_id} {r.item_name} x{int(r.quantity):,} @ {int(r.value):,}: {why}")
    print(f"\n  total {old_total:,} -> {new_total:,} gp ({new_total - old_total:+,})")
    if not plan:
        return 0
    drop_ids = [int(r.drop_id) for r, _n, _w in plan]
    splits = db.execute(text("SELECT COUNT(*) FROM drop_splits WHERE drop_id IN :ids")
                        .bindparams(bindparam("ids", expanding=True)), {"ids": drop_ids}).scalar()
    if splits:
        sys.exit(f"{splits} drop_splits rows reference these drops; handle them first.")
    if not args.apply:
        print("\nRe-run with --apply to write.")
        return 0

    backup = os.path.join(REPO_ROOT, "scripts", "data",
                          f"fix_broadcast_quantities_{datetime.utcnow():%Y%m%dT%H%M%S}.json")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    with open(backup, "w") as fh:
        json.dump([{"drop_id": int(r.drop_id), "quantity": int(r.quantity), "new": new}
                   for r, new, _w in plan], fh, indent=1)
    print(f"\nPrevious quantities saved to {backup}")
    for r, new, _w in plan:
        db.execute(text("UPDATE drops SET quantity = :q WHERE drop_id = :id AND quantity = :old"),
                   {"q": new, "id": int(r.drop_id), "old": int(r.quantity)})
    db.commit()
    print(f"Updated {len(plan)} drops.")

    touched = [r for r, _n, _w in plan]
    players = sorted({int(r.player_id) for r in touched})
    for pid, item_id, npc_id, hour, part in {
        (int(r.player_id), int(r.item_id), int(r.npc_id), r.date_added.strftime("%Y-%m-%d-%H"),
         _partition_of(r.date_added)) for r in touched
    }:
        db.execute(_REFOLD_ITEM_CELL_SQL, {"player_id": pid, "item_id": item_id,
                                           "date_hour": hour, "partition": part})
        db.execute(_REFOLD_NPC_CELL_SQL, {"player_id": pid, "npc_id": npc_id,
                                          "date_hour": hour, "partition": part})
    db.commit()
    print("Re-folded hourly rollup cells.")

    for pid in players:
        try:
            force_update_player(pid, db)
        except Exception as exc:
            print(f"  ! force_update_player({pid}) failed: {exc}", flush=True)
            db.rollback()
    print(f"Rebuilt Redis totals for {len(players)} players.")

    memberships = defaultdict(set)
    for pid, g in db.execute(text(
            "SELECT player_id, group_id FROM user_group_association WHERE player_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True)), {"ids": players}).fetchall():
        memberships[int(pid)].add(int(g))
    pairs = sorted({(int(r.player_id), int(r.npc_id)) for r in touched})
    pipe = conn.pipeline(transaction=False)
    ttl = RedisLootTracker._NPC_MONTH_TTL
    for pid, npc_id in pairs:
        all_time, monthly = 0, {}
        for part, total in db.execute(text(
            "SELECT YEAR(date_added)*100 + MONTH(date_added), SUM(value*quantity) "
            "FROM drops FORCE INDEX (ix_drops_npc_id) "
            "WHERE npc_id = :npc AND player_id = :pid AND hidden != 1 GROUP BY 1"
        ), {"npc": npc_id, "pid": pid}).fetchall():
            all_time += int(total)
            monthly[int(part)] = int(total)
        for base in [f"leaderboard:npc:{npc_id}"] + [
                f"leaderboard:group:{g}:npc:{npc_id}" for g in sorted(memberships[pid])]:
            pipe.zadd(base, {str(pid): all_time})
            for part, total in monthly.items():
                pipe.zadd(f"{base}:{part}", {str(pid): total})
                pipe.expire(f"{base}:{part}", ttl)
    pipe.execute()
    print(f"Set per-NPC board scores for {len(pairs)} (player, npc) pairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
