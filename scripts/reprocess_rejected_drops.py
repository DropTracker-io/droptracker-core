"""Reprocess drops that were wrongly rejected by the >1M item/NPC wiki check.

Idempotent: skips any drop whose unique_id already exists. For each drop it
re-runs the DB-insert + Redis-leaderboard update that the drop processor would
have done (db.ops.create_drop_object + services.redis_updates.add_to_player),
WITHOUT re-sending stale Discord notifications. It does NOT re-run the wiki
verification (that's the bug being worked around) or WOM auth (the player is
already known/authed).

Edit DROPS below, then run:
    ./venv/bin/python3 -m scripts.reprocess_rejected_drops --dry-run
    ./venv/bin/python3 -m scripts.reprocess_rejected_drops --commit
"""

import argparse
import asyncio
from datetime import datetime

# Each entry is one drop to restore. value=None -> resolve current GE value.
#   player_id, item_id, npc_id, quantity, date (ISO), unique_id, value
DROPS: list[dict] = [
    # Example (the Redis-confirmed Chefs Hat rejection). Confirm before enabling.
    # {"player_id": 1837981, "item_id": 30631, "npc_id": 13980, "quantity": 1,
    #  "date": "2026-07-15 03:26:36",
    #  "unique_id": "1784085995--2452108049223311531-4665515883775371637",
    #  "value": None},
]


async def _resolve_value(item_name, item_id, submitted_value=0):
    from utils.ge_value import get_true_item_value
    try:
        return int(await get_true_item_value(item_name, int(submitted_value or 0), item_id=item_id))
    except Exception:
        return int(submitted_value or 0)


async def _reprocess_one(d: dict, commit: bool) -> str:
    from db import Drop, Player, ItemList, NpcList, session
    from db.ops import DatabaseOperations
    from services import redis_updates

    uid = d["unique_id"]
    existing = session.query(Drop).filter(Drop.unique_id == uid).first()
    if existing:
        return f"SKIP already present (drop_id={existing.drop_id})"

    player = session.query(Player).filter(Player.player_id == d["player_id"]).first()
    if not player:
        return f"SKIP player {d['player_id']} not found"
    item = session.query(ItemList).filter(ItemList.item_id == d["item_id"]).first()
    npc = session.query(NpcList).filter(NpcList.npc_id == d["npc_id"]).first()
    if not item or not npc:
        return f"SKIP item/npc missing (item={bool(item)} npc={bool(npc)})"

    value = d.get("value")
    if value is None:
        value = await _resolve_value(item.item_name, item.item_id)
    quantity = int(d.get("quantity", 1))
    date_received = datetime.fromisoformat(d["date"]) if isinstance(d.get("date"), str) else d.get("date") or datetime.now()

    if not commit:
        return (f"WOULD INSERT {item.item_name} x{quantity} from {npc.npc_name} "
                f"value={value} date={date_received} uid={uid}")

    db = DatabaseOperations()
    drop = await db.create_drop_object(
        item_id=item.item_id, player_id=player.player_id, date_received=date_received,
        npc_id=npc.npc_id, value=int(value), quantity=quantity,
        authed=True, used_api=True, unique_id=uid,
    )
    if not drop:
        return "ERROR create_drop_object returned None"
    try:
        redis_updates.add_to_player(player, drop, item_name=item.item_name, npc_name=npc.npc_name)
    except Exception as e:
        return f"INSERTED drop_id={drop.drop_id} but redis update failed: {e}"
    return f"INSERTED drop_id={drop.drop_id} {item.item_name} x{quantity} value={value}"


async def _main(commit: bool):
    if not DROPS:
        print("DROPS is empty — nothing to do. Populate it first.")
        return
    for d in DROPS:
        print(await _reprocess_one(d, commit))


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    asyncio.run(_main(commit=args.commit))


if __name__ == "__main__":
    main()
