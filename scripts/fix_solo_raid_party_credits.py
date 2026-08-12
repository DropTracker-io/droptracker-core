#!/usr/bin/env python3
"""
Reverse group point-shares that a SOLO raid handed to RuneLite party members.

What happened
-------------
The plugin's ``NearbyPlayerTracker`` attaches an authoritative raid roster
(ToB/ToA varcstrings, CoX raiding-party sidepanel) to raid submissions, and
falls back to guessing when that roster comes back empty. A solo raid empties
the roster by definition -- the local player is stripped from it -- so it was
indistinguishable from a failed capture, and the fallback ran anyway. That
fallback folded in ``PartyService.getMembers()``, and RuneLite party membership
survives a member logging out of the game entirely. Anyone still in the
submitter's party from an earlier session was therefore credited as a raid
participant.

Server side the payload is trusted as-is: ``data/submissions/drop.py`` reads
``nearby_players`` into ``players_included`` and hands it to
``check_and_award_points``, which splits the drop's award across the receiver
plus every named participant who belongs to the group (``point_sharing=1``).

Fixed plugin-side by ``NearbyPlayerTracker.isSoloRaid`` (solo is now proven
from the game's own party size / a demonstrably live capture, and an empty
participant list is kept) plus dropping PartyService from the raid fallback.

What this script does
---------------------
For each confirmed event: delete the drop's ``player_points`` rows for that
group and re-run the production award pipeline with an EMPTY participant list,
which is what the engine would have done had the payload been correct. Using
``check_and_award_points`` rather than hand-editing amounts keeps every group
point mod, bound and config in play.

Only group points are affected. ``split_gp_tracking`` was not enabled for any
of these groups, so no ``drop_splits`` rows exist and the GP leaderboards were
never touched.

Confirmed events
----------------
Candidates were every raid-source drop whose award was shared (231 credit rows
across 102 events, 2026-04-05 onward), narrowed to those where NO credited
participant submitted their own loot from the same raid within +/-240s -- a
genuine co-raider who runs the plugin always does. Each of those 15 was then
checked against the receiver's own screenshot. Fourteen are real team raids
(party-size overlays, sub-100%% personal points, teammates visible in frame).
One is a solo raid:

  drop 187463658 -- SirGoki, Arcane prayer scroll, Chambers of Xeric,
  2026-08-11 18:26:18. Screenshot: "Total points: 58,340, Personal points:
  58,340 (100%%)", nobody else in the treasure room. Credited "Mike Lamitch",
  whose last submission was 57 minutes earlier in an unrelated ToB.

Usage
-----
    ./venv/bin/python -m scripts.fix_solo_raid_party_credits            # dry run
    ./venv/bin/python -m scripts.fix_solo_raid_party_credits --apply

Idempotent: a re-run re-reads live state, and an event whose award already
matches the no-split result is reported as already-corrected and skipped.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO_ROOT, ".env"))

from db.models import Drop, NpcList, Player, PlayerPoints, session  # noqa: E402
from services.entry_modifier import _re_award_points  # noqa: E402

LOG_DIR = os.path.join(REPO_ROOT, "logs")

# (drop_id, group_id) confirmed above. Keep the provenance in the docstring.
CONFIRMED = [
    (187463658, 190),
]


def _describe(db, drop_id, group_id):
    drop = db.query(Drop).filter(Drop.drop_id == drop_id).first()
    if drop is None:
        return None
    npc = db.query(NpcList).filter(NpcList.npc_id == drop.npc_id).first()
    receiver = db.query(Player).filter(Player.player_id == drop.player_id).first()
    rows = (
        db.query(PlayerPoints)
        .filter(PlayerPoints.entry_id == drop_id, PlayerPoints.group_id == group_id)
        .order_by(PlayerPoints.id)
        .all()
    )
    detail = []
    for r in rows:
        name = db.query(Player.player_name).filter(Player.player_id == r.player_id).scalar()
        detail.append({
            "id": r.id, "player_id": r.player_id, "player_name": name,
            "amount": int(r.amount), "reason": r.reason,
        })
    return {
        "drop_id": drop_id, "group_id": group_id,
        "npc_name": npc.npc_name if npc else None,
        "receiver_id": drop.player_id,
        "receiver": receiver.player_name if receiver else None,
        "date_added": str(drop.date_added),
        "rows": detail,
    }


async def _fix_one(db, drop_id, group_id, apply):
    before = _describe(db, drop_id, group_id)
    if before is None:
        print(f"  drop {drop_id}: NOT FOUND, skipping")
        return None

    print(f"\n  drop {drop_id} ({before['npc_name']}) -> {before['receiver']} "
          f"on {before['date_added']}, group {group_id}")
    for r in before["rows"]:
        print(f"    before: {r['player_name']:<20} {r['amount']:>5} pts   [{r['reason']}]")

    shared = [r for r in before["rows"] if r["player_id"] != before["receiver_id"]]
    if not shared:
        print("    already corrected (no non-receiver credit rows) -- skipping")
        return {"before": before, "after": before, "skipped": True}

    if not apply:
        total = sum(r["amount"] for r in before["rows"])
        print(f"    DRY RUN: would delete {len(before['rows'])} row(s) and re-award "
              f"with no participants (receiver should end up with ~{total} pts)")
        return {"before": before, "after": None, "skipped": False}

    drop = db.query(Drop).filter(Drop.drop_id == drop_id).first()
    db.query(PlayerPoints).filter(
        PlayerPoints.entry_id == drop_id,
        PlayerPoints.group_id == group_id,
    ).delete(synchronize_session="fetch")
    db.flush()

    # Empty participant list: exactly what a correct payload would have carried.
    await _re_award_points(drop, group_id, [], db)
    db.commit()

    after = _describe(db, drop_id, group_id)
    for r in after["rows"]:
        print(f"    after:  {r['player_name']:<20} {r['amount']:>5} pts   [{r['reason']}]")
    return {"before": before, "after": after, "skipped": False}


async def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the corrections (default is a dry run)")
    ap.add_argument("--drop-id", type=int, action="append",
                    help="restrict to these drop ids (default: the confirmed list)")
    args = ap.parse_args(argv)

    targets = CONFIRMED
    if args.drop_id:
        wanted = set(args.drop_id)
        targets = [t for t in CONFIRMED if t[0] in wanted]
        missing = wanted - {t[0] for t in CONFIRMED}
        if missing:
            print(f"ERROR: {sorted(missing)} not in the confirmed list; add them there "
                  f"with the evidence that the raid was solo.")
            return 1

    print(f"{'APPLY' if args.apply else 'DRY RUN'}: {len(targets)} event(s)")
    db = session()
    results = []
    try:
        for drop_id, group_id in targets:
            try:
                results.append(await _fix_one(db, drop_id, group_id, args.apply))
            except Exception as e:
                db.rollback()
                print(f"  drop {drop_id}: FAILED, rolled back: {e}")
                return 1
    finally:
        db.close()

    if args.apply:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = os.path.join(LOG_DIR, f"solo_raid_party_credits_{stamp}.json")
        with open(out, "w") as fh:
            json.dump(results, fh, indent=1, default=str)
        print(f"\nBefore/after snapshot: {out}")
        print("Rollback: re-insert the `before` rows from that snapshot.")
    else:
        print("\nRe-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
