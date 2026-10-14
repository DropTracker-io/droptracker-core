"""Move chat drops off the "Clan Broadcast" sentinel NPC onto their real source.

Why this exists
---------------
Clan broadcast lines only sometimes say where the loot came from. Until the
sourceless-drop inference landed (``db.item_sources.infer_sole_source``), every
line without a "from X" clause was stored under the sentinel npc_list row
"Clan Broadcast". That made it a fake boss with thousands of drops, in search,
"top bosses" tiles and item source lists. This applies the same inference the
processor now uses to the rows already stored.

What it does
------------
For each item held by the sentinel, ask ``infer_sole_source``. Items with one
clear source are moved; genuinely multi-source items (Dragon platelegs,
revenant loot, raid-mode purples) stay on the sentinel, which read paths now
skip. For a moved item, in one transaction:

  * ``drops.npc_id`` and ``group_recent_drops.npc_id`` -> the inferred NPC
  * ``player_npc_hourly_totals``: the moved drops are added to the NPC's
    hourly rows (same grouping as ``services/npc_totals.py``)

Then every sentinel row is deleted from ``player_npc_hourly_totals``: the
tailer stopped writing it, and its leftovers would keep the sentinel on
"top bosses" surfaces.

Usage
-----
    python -m scripts.reattribute_clan_broadcast_drops           # dry run
    python -m scripts.reattribute_clan_broadcast_drops --apply   # write

Idempotent: moved rows no longer sit on the sentinel, so a rerun only picks
up drops recorded since (none, once the processor change is live) and items
whose evidence has since become clear.
"""

import argparse
import sys

sys.path.insert(0, ".")

from sqlalchemy import bindparam, text

from db.item_sources import infer_sole_source, item_ids_for_name
from db.models.base import Session
from utils.clan_broadcasts import SENTINEL_NPC_NAME

HOURLY = "player_npc_hourly_totals"


def _resolve(s, name):
    """Inferred name -> npc_list row, by the processor's own resolver."""
    from data.submissions.clan_broadcast import _resolve_npc

    return _resolve_npc(s, name)


def plan(s, sentinel_id):
    rows = s.execute(
        text(
            "SELECT i.item_name, COUNT(*) FROM drops d "
            "JOIN items i ON i.item_id = d.item_id "
            "WHERE d.npc_id = :s GROUP BY i.item_name ORDER BY COUNT(*) DESC"
        ),
        {"s": sentinel_id},
    ).fetchall()
    out = []
    for item_name, count in rows:
        ids = item_ids_for_name(s, item_name)
        source = infer_sole_source(s, ids, exclude_npc_ids=[sentinel_id])
        npc_id, npc_name = _resolve(s, source) if source else (None, None)
        out.append((item_name, ids, int(count), npc_id, npc_name))
    return out


def move_item(s, sentinel_id, item_ids, npc_id) -> int:
    drop_ids = [
        int(d) for (d,) in s.execute(
            text("SELECT drop_id FROM drops WHERE npc_id = :s AND item_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"s": sentinel_id, "ids": item_ids},
        ).fetchall()
    ]
    if not drop_ids:
        return 0
    params = {"n": npc_id, "ids": drop_ids}
    s.execute(
        text("UPDATE drops SET npc_id = :n WHERE drop_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        params,
    )
    s.execute(
        text("UPDATE group_recent_drops SET npc_id = :n WHERE drop_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        params,
    )
    s.execute(
        text(
            f"INSERT INTO {HOURLY} "
            "  (player_id, npc_id, date_hour, `partition`, total_value, drop_count, last_drop_time) "
            "SELECT d.player_id, d.npc_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), "
            "       COALESCE(d.`partition`, CAST(DATE_FORMAT(d.date_added, '%Y%m') AS UNSIGNED)), "
            "       SUM(d.value * d.quantity), COUNT(*), MAX(d.date_added) "
            "FROM drops d WHERE d.drop_id IN :ids AND d.date_added IS NOT NULL "
            "GROUP BY d.player_id, d.npc_id, DATE_FORMAT(d.date_added, '%Y-%m-%d-%H'), "
            "         COALESCE(d.`partition`, CAST(DATE_FORMAT(d.date_added, '%Y%m') AS UNSIGNED)) "
            "ON DUPLICATE KEY UPDATE "
            "  total_value = total_value + VALUES(total_value), "
            "  drop_count = drop_count + VALUES(drop_count), "
            "  last_drop_time = GREATEST(COALESCE(last_drop_time, VALUES(last_drop_time)), "
            "                            VALUES(last_drop_time))"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": drop_ids},
    )
    return len(drop_ids)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    s = Session()
    try:
        sentinel_id = s.execute(
            text("SELECT npc_id FROM npc_list WHERE npc_name = :n"), {"n": SENTINEL_NPC_NAME}
        ).scalar()
        if sentinel_id is None:
            print("No sentinel row; nothing to do.")
            return
        rows = plan(s, int(sentinel_id))
        moved = kept = 0
        for item_name, _ids, count, npc_id, npc_name in rows:
            if npc_id:
                moved += count
                print(f"  move {count:5}  {item_name}  ->  {npc_name} ({npc_id})")
            else:
                kept += count
        for item_name, _ids, count, npc_id, _n in rows:
            if not npc_id:
                print(f"  keep {count:5}  {item_name}  (no single source)")
        stale = s.execute(
            text(f"SELECT COUNT(*) FROM {HOURLY} WHERE npc_id = :s"), {"s": sentinel_id}
        ).scalar()
        print(f"\n{moved} drops to move, {kept} stay on the sentinel; "
              f"{stale} sentinel rollup rows to delete.")
        s.rollback()
        if not args.apply:
            print("Dry run. Re-run with --apply to write.")
            return

        done = 0
        for item_name, ids, _count, npc_id, npc_name in rows:
            if not npc_id:
                continue
            try:
                n = move_item(s, int(sentinel_id), ids, int(npc_id))
                s.commit()
                done += n
            except Exception as e:
                s.rollback()
                print(f"  FAILED {item_name} -> {npc_name}: {e}")
        s.execute(text(f"DELETE FROM {HOURLY} WHERE npc_id = :s"), {"s": sentinel_id})
        s.commit()
        print(f"Moved {done} drops; sentinel rollup rows deleted.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
