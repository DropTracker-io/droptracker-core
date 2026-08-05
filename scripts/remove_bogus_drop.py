"""Remove one erroneously-accepted drop and reverse every side effect.

Built for the 2026-08-05 "Tumeken's shadow from Dossier" incident (drop
182453036): a RuneLite container-open inventory diff produced a phantom 782M
drop that passed the high-value check through the charged/uncharged dropsline
gap (fixed in osrs_api/semantic.py the same day). Kept generic so the next
bad acceptance is a one-liner.

Mirrors the admin "Modify Entry -> Delete" sequence in
services/entry_modifier.py (points, splits, notified rows, drop row, Redis
force-rebuild, Discord messages), and additionally repairs the two surfaces
that sequence doesn't touch:

  * per-NPC leaderboards (leaderboard:npc:* and the per-group variants) —
    force_update_player() rebuilds the loot boards but not these, so the
    phantom value would linger there forever;
  * the player_npc_hourly_totals / player_item_hourly_totals rollups — their
    additive tailer has already folded the drop in by the time anyone
    notices, and it never revisits folded hours on its own.

Usage:
    python -m scripts.remove_bogus_drop --drop-id 182453036          # dry run
    python -m scripts.remove_bogus_drop --drop-id 182453036 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import text

load_dotenv()


def _fmt_gp(value: int) -> str:
    return f"{value:,} GP"


def _npc_board_keys(npc_id: int, partition: int, group_ids: list[int]) -> list[str]:
    keys = [
        f"leaderboard:npc:{npc_id}:{partition}",
        f"leaderboard:npc:{npc_id}",
    ]
    for gid in group_ids:
        keys.append(f"leaderboard:group:{gid}:npc:{npc_id}:{partition}")
        keys.append(f"leaderboard:group:{gid}:npc:{npc_id}")
    return keys


def _true_npc_totals(session, player_id: int, npc_id: int, partition: int) -> tuple[int, int]:
    """(this-partition total, all-time total) for player+npc from `drops`."""
    month = session.execute(
        text(
            "SELECT COALESCE(SUM(value * quantity), 0) FROM drops "
            "WHERE player_id = :pid AND npc_id = :nid AND `partition` = :part"
        ),
        {"pid": player_id, "nid": npc_id, "part": partition},
    ).scalar()
    all_time = session.execute(
        text(
            "SELECT COALESCE(SUM(value * quantity), 0) FROM drops "
            "WHERE player_id = :pid AND npc_id = :nid"
        ),
        {"pid": player_id, "nid": npc_id},
    ).scalar()
    return int(month or 0), int(all_time or 0)


def _delete_discord_message(channel_id: str, message_id: str) -> bool:
    """Best-effort REST delete (no gateway needed for message deletion)."""
    import urllib.request

    token = os.getenv("BOT_TOKEN")
    if not token or not channel_id or not message_id:
        return False
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
        method="DELETE",
        headers={
            "Authorization": f"Bot {token}",
            # Cloudflare fronts discord.com and 403s urllib's default UA
            # (error code 1010); Discord also wants the DiscordBot form.
            "User-Agent": "DiscordBot (https://www.droptracker.io, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"  ! could not delete Discord message {message_id} in {channel_id}: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drop-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument(
        "--keep-discord-messages",
        action="store_true",
        help="skip deleting the group notification messages on Discord",
    )
    args = parser.parse_args()

    from db.models import Drop, ItemList, NpcList, Player, NotifiedSubmission
    from db.models.base import session
    from db.models.drop_split import DropSplit
    from db.models.group_points import PlayerPoints

    drop = session.query(Drop).filter(Drop.drop_id == args.drop_id).first()
    if not drop:
        print(f"Drop {args.drop_id} not found — nothing to do.")
        return 1

    player = session.query(Player).filter(Player.player_id == drop.player_id).first()
    item = session.query(ItemList).filter(ItemList.item_id == drop.item_id).first()
    npc = session.query(NpcList).filter(NpcList.npc_id == drop.npc_id).first()
    group_ids = [g.group_id for g in (player.groups if player else [])]
    total_value = int(drop.value) * int(drop.quantity)

    print(f"Drop {drop.drop_id}: {drop.quantity} x "
          f"{item.item_name if item else drop.item_id} from "
          f"{npc.npc_name if npc else drop.npc_id} — {_fmt_gp(total_value)}")
    print(f"  player: {player.player_name if player else '?'} ({drop.player_id}), "
          f"groups {group_ids}, partition {drop.partition}, added {drop.date_added}")

    points_rows = (
        session.query(PlayerPoints)
        .filter(PlayerPoints.entry_id == drop.drop_id, PlayerPoints.reason == "drop")
        .all()
    )
    notified_rows = (
        session.query(NotifiedSubmission)
        .filter(NotifiedSubmission.drop_id == drop.drop_id)
        .all()
    )
    split_rows = session.query(DropSplit).filter(DropSplit.drop_id == drop.drop_id).all()
    split_player_ids = sorted({r.player_id for r in split_rows})

    for row in points_rows:
        print(f"  will delete point award: {row.amount} pts to player {row.player_id} "
              f"in group {row.group_id} (player_points.id={row.id})")
    for row in notified_rows:
        print(f"  will delete notified row {row.id} (group {row.group_id}, "
              f"message {row.message_id} in channel {row.channel_id})"
              + ("" if args.keep_discord_messages else " + the Discord message"))
    if split_rows:
        print(f"  will delete {len(split_rows)} drop_splits rows and force-rebuild "
              f"split participants {split_player_ids}")
    print(f"  will delete the drop row, force-rebuild Redis for player {drop.player_id}, "
          f"repair NPC boards for npc {drop.npc_id}, and refold hourly rollups for "
          f"{drop.date_added.date()}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to execute.")
        return 0

    # Capture everything needed after the ORM rows are gone.
    receiver_id = int(drop.player_id)
    npc_id = int(drop.npc_id)
    item_id_for_rollup = int(drop.item_id)
    partition = int(drop.partition)
    drop_day = drop.date_added.date()
    messages = [(r.channel_id, r.message_id) for r in notified_rows]

    # 1. DB rows: points, splits, notified, then the drop itself.
    for row in points_rows:
        session.delete(row)
    if split_rows:
        session.query(DropSplit).filter(DropSplit.drop_id == args.drop_id).delete(
            synchronize_session="fetch"
        )
    if notified_rows:
        session.query(NotifiedSubmission).filter(
            NotifiedSubmission.drop_id == args.drop_id
        ).delete(synchronize_session="fetch")
    session.query(Drop).filter(Drop.drop_id == args.drop_id).delete(
        synchronize_session="fetch"
    )
    session.commit()
    print("DB rows deleted.")

    # 2. Rebuild the receiver's (and any split participants') Redis state from
    #    the now-corrected drops table.
    from services.redis_updates import RedisLootTracker

    tracker = RedisLootTracker()
    for pid in [receiver_id, *split_player_ids]:
        ok = tracker.force_update_player(pid, session_to_use=session)
        print(f"Redis force-rebuild for player {pid}: {'ok' if ok else 'FAILED'}")

    # 3. Per-NPC boards (not covered by the force rebuild): write the player's
    #    true remaining totals, removing the member when the total is zero.
    from utils.redis import RedisClient

    redis = RedisClient().client
    month_total, all_time_total = _true_npc_totals(session, receiver_id, npc_id, partition)
    for key in _npc_board_keys(npc_id, partition, group_ids):
        score = month_total if key.endswith(f":{partition}") else all_time_total
        if score > 0:
            redis.zadd(key, {str(receiver_id): score})
        else:
            redis.zrem(key, str(receiver_id))
    print(f"NPC boards repaired: npc {npc_id} month={_fmt_gp(month_total)} "
          f"all-time={_fmt_gp(all_time_total)}")

    # 4. Refold the hourly rollup tables for the affected day so the phantom
    #    value leaves player_npc_hourly_totals / player_item_hourly_totals.
    #    The refold upsert only rewrites buckets that still have backing
    #    drops — a bucket whose drops were ALL deleted is never touched — so
    #    the affected player's buckets are deleted first and the refold
    #    recreates whatever legitimately remains.
    from services import item_totals, npc_totals

    day_start = datetime.combine(drop_day, datetime.min.time())
    day_end = day_start + timedelta(days=1)
    day_prefix = drop_day.strftime("%Y-%m-%d")
    session.execute(
        text(
            "DELETE FROM player_npc_hourly_totals "
            "WHERE player_id = :pid AND npc_id = :nid AND date_hour LIKE :day"
        ),
        {"pid": receiver_id, "nid": npc_id, "day": f"{day_prefix}-%"},
    )
    session.execute(
        text(
            "DELETE FROM player_item_hourly_totals "
            "WHERE player_id = :pid AND item_id = :iid AND date_hour LIKE :day"
        ),
        {"pid": receiver_id, "iid": item_id_for_rollup, "day": f"{day_prefix}-%"},
    )
    session.commit()
    for mod, label in ((npc_totals, "npc"), (item_totals, "item")):
        ceiling = mod.get_pointer()
        if ceiling is None:
            print(f"  ! {label} rollup has no pointer; skipping refold")
            continue
        rows = mod.refold_day(session, partition, day_start, day_end, max_drop_id=ceiling)
        print(f"Refolded {label} hourly rollup for {drop_day}: {rows} rows touched")

    # 5. Take down the group notification messages.
    if not args.keep_discord_messages:
        for channel_id, message_id in messages:
            if _delete_discord_message(channel_id, message_id):
                print(f"Deleted Discord message {message_id} in channel {channel_id}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
