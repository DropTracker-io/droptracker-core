#!/usr/bin/env python3
"""
Award the clan points that raid loot was wrongly refused, 2026-11-16/17.

What happened
-------------
Plugin 6.1.27 sends ``in_raid`` and ``in_party`` with raid loot, and the server
refuses clan points for loot claimed outside the raid with no RuneLite party
open (``data/submissions/drop.py::raid_loot_points_eligible``, rule agreed
2026-11-13). Tombs of Amascut and the Fractured Archive drop their in-raid
state before the chest is opened, so the plugin reported ``in_raid=false`` for
loot claimed INSIDE their loot rooms, and every such chest whose owner had no
party open earned nothing. Reported by Sokira2 (#questions, 2026-11-17); the
owner confirmed from the screenshots that both reported raids were solo and
looted inside. Fixed in disc 8c689b5 ("ToA and the Fractured Archive are not judged
by in_raid"); this restores what was withheld before that went live.

Which drops
-----------
Every drop the consumer journal logged as "raid loot claimed outside the raid
with no party open; no points" between the plugin's release and the fix,
passed in as a JSON file (``--guids``) with two lists of submission GUIDs:

* ``no_participants``: the payload named nobody else, so the award is the
  receiver's alone. These are re-awarded here, through the production
  pipeline with an empty participant list, which is exactly what the drop
  processor would have done.
* ``named_team``: the payload named co-raiders. The names were not kept
  (``drops`` has no participants column), so the split cannot be replayed and
  these are only LISTED, never awarded.

For each drop, every group the receiver belongs to whose points system is on
and which holds no ``player_points`` row for the drop gets the award. A drop
worth nothing under a group's rules awards nothing, as it would have.

Usage
-----
    ./venv/bin/python -m scripts.restore_raid_loot_points --guids FILE          # dry run
    ./venv/bin/python -m scripts.restore_raid_loot_points --guids FILE --apply

Idempotent: a group that already holds a points row for the drop is skipped.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO_ROOT, ".env"))

from db.models import Drop, ItemList, NpcList, Player, PlayerPoints, session  # noqa: E402


def _describe(db, drop):
    item = db.query(ItemList.item_name).filter(ItemList.item_id == drop.item_id).scalar()
    npc = db.query(NpcList.npc_name).filter(NpcList.npc_id == drop.npc_id).scalar()
    who = db.query(Player.player_name).filter(Player.player_id == drop.player_id).scalar()
    return (f"drop {drop.drop_id} {drop.date_added} {who}: {item} x{drop.quantity} "
            f"({int(drop.value) * int(drop.quantity):,} gp) from {npc}")


async def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--guids", required=True, help="JSON: {no_participants: [...], named_team: [...]}")
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args(argv)

    from data.submissions.common import check_group_point_system_active
    from data.submissions.point_awards import check_and_award_points

    with open(args.guids) as fh:
        guids = json.load(fh)
    db = session
    awards = total = 0

    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(guids['no_participants'])} drops to re-award, "
          f"{len(guids['named_team'])} named-team drops listed only\n")

    for guid in guids["no_participants"]:
        drop = db.query(Drop).filter(Drop.unique_id == guid).first()
        if drop is None:
            print(f"  guid {guid}: no drop row, skipping")
            continue
        player = db.query(Player).filter(Player.player_id == drop.player_id).first()
        value = int(drop.value) * int(drop.quantity)
        for group in list(player.groups):
            gid = group.group_id
            if not check_group_point_system_active(gid, db):
                continue
            held = (db.query(PlayerPoints.id)
                    .filter(PlayerPoints.entry_id == drop.drop_id, PlayerPoints.group_id == gid,
                            PlayerPoints.reason.like("drop%")).first())
            if held:
                print(f"  {_describe(db, drop)} | group {gid}: already holds a points row, skipping")
                continue
            result = await check_and_award_points(
                "drop", gid, drop.player_id, value, players_included=[],
                item_id=drop.item_id, npc_id=drop.npc_id, quantity=int(drop.quantity),
                entry_id=drop.drop_id, external_session=db, dry_run=not args.apply,
            )
            got = int((result or {}).get("receiver_points_awarded", 0) or 0)
            if got:
                awards += 1
                total += got
                print(f"  {_describe(db, drop)} | group {gid} ({group.group_name}): "
                      f"{'awarded' if args.apply else 'would award'} {got} pts")
        if args.apply:
            db.commit()
        else:
            db.rollback()

    print(f"\n{'Awarded' if args.apply else 'Would award'} {total} points across {awards} drop/group awards.")

    if guids["named_team"]:
        print("\nNamed-team drops (NOT awarded: the co-raiders' names were not kept):")
        for guid in guids["named_team"]:
            drop = db.query(Drop).filter(Drop.unique_id == guid).first()
            if drop is not None and int(drop.value) * int(drop.quantity) > 0:
                print(f"  {_describe(db, drop)}")
    if not args.apply:
        print("\nRe-run with --apply to write.")


if __name__ == "__main__":
    asyncio.run(main())
