#!/usr/bin/env python3
"""Record the clan-broadcast pets lost after the 2026-11-18 game update.

Since that update the pronoun and the number in pet broadcasts arrive stripped
(tag variables). Until 74c5268 (2026-11-25 ~14:52 UTC) two things went wrong:

* Lines that still parsed kept the husk in the pet name ("Beef at kills",
  "Orion at completions"). No item resolves to that name, so the pet was posted
  with a broken title and never written to ``player_pets``. Those posts are in
  ``notification_queue`` (pet, via_clan_broadcast, item_id null) with the player
  id and the original game line -- only for groups with pet notifications on.
* "sneaking into backpack" / "feels like acquired" lines did not parse at all
  and were dropped. The consumer journal keeps them as
  ``[ClanBroadcast] Unknown broadcast shape: '...'`` (one log per distinct line
  per hour, so repeats of the same broadcast from several relayers collapse,
  which is what we want). The journal on this box starts 2026-11-20 13:42 UTC.

Each line is re-parsed with the fixed parser. A pet is written only when the
player can be resolved, the pet name resolves to an item, and the player has no
row for that item yet (whichever relay recorded it first wins). No Discord posts,
no points: the live clan-broadcast pet path awards none either.

Dry-run by default (rolls back); ``--apply`` commits. Idempotent: re-running
finds the rows already there. ``unique_id`` is ``ccbf:<sha>`` of player+item.

Usage:
  ./venv/bin/python -m scripts.backfill_lost_clan_pets            # dry-run
  ./venv/bin/python -m scripts.backfill_lost_clan_pets --apply
"""
import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime

from sqlalchemy import text

SINCE = "2026-11-18 00:00:00"
# The consumer + webhooks bot restart that shipped 74c5268.
UNTIL = "2026-11-25 14:53:00"
UNITS = ("droptracker-webhook-consumer", "droptracker-webhooks")
_UNKNOWN_RE = re.compile(
    r"^(?P<ts>\S+) \S+ \S+: \[ClanBroadcast\] Unknown broadcast shape: (?P<lit>.+)$"
)


def journal_lines():
    """(timestamp, line) for every unknown-shape broadcast in the window."""
    out = []
    for unit in UNITS:
        res = subprocess.run(
            ["journalctl", "-u", unit, "--since", SINCE, "--until", UNTIL,
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, check=False,
        )
        for raw in res.stdout.splitlines():
            m = _UNKNOWN_RE.match(raw)
            if not m:
                continue
            try:
                line = ast.literal_eval(m["lit"])
                ts = datetime.fromisoformat(m["ts"]).replace(tzinfo=None)
            except Exception:
                continue
            out.append((ts, line))
    return out


def queue_lines(session):
    """(timestamp, player_id, line, queue_id) for pet posts that lost their item."""
    rows = session.execute(text(
        """SET STATEMENT max_statement_time=30 FOR
           SELECT id, player_id, created_at,
                  JSON_UNQUOTE(JSON_EXTRACT(data, '$.game_message')) AS msg
           FROM notification_queue
           WHERE notification_type = 'pet'
             AND created_at >= :since AND created_at < :until
             AND JSON_EXTRACT(data, '$.via_clan_broadcast') = true
             AND (JSON_EXTRACT(data, '$.item_id') IS NULL
                  OR JSON_TYPE(JSON_EXTRACT(data, '$.item_id')) = 'NULL')"""
    ), {"since": SINCE, "until": UNTIL}).all()
    return [(r.created_at, r.player_id, r.msg, r.id) for r in rows if r.msg]


def resolve_item_id(session, name, cache):
    if name in cache:
        return cache[name]
    from data.submissions.common import game_item_id_for_name

    row = session.execute(
        text("SELECT item_id FROM items WHERE item_name = :n ORDER BY item_id LIMIT 1"),
        {"n": name},
    ).first()
    item_id = row[0] if row else None
    if item_id is None:
        try:
            item_id = game_item_id_for_name(name)
        except Exception:
            item_id = None
    cache[name] = item_id
    return item_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit (default: dry-run)")
    args = ap.parse_args()

    from db.models import Player, PlayerPet, session
    from utils.clan_broadcasts import parse_broadcast
    from utils.rsn import find_player_by_rsn

    candidates = []   # (ts, player_id, pet_name, origin)
    skipped = []

    for ts, pid, msg, qid in queue_lines(session):
        parsed = parse_broadcast(msg)
        if parsed is None or parsed.kind != "pet":
            skipped.append(("queue: does not parse as a pet", qid, msg))
            continue
        candidates.append((ts, pid, parsed.item_name, f"queue#{qid}"))

    for ts, line in journal_lines():
        parsed = parse_broadcast(line)
        if parsed is None or parsed.kind != "pet":
            continue  # not a pet line: other kinds are not this backfill
        player = find_player_by_rsn(session, Player, parsed.player)
        if player is None:
            skipped.append(("journal: no player row", parsed.player, line))
            continue
        candidates.append((ts, player.player_id, parsed.item_name, "journal"))
    session.rollback()

    item_cache = {}
    best = {}   # (player_id, item_id) -> earliest candidate
    for ts, pid, pet, origin in sorted(candidates, key=lambda c: c[0]):
        item_id = resolve_item_id(session, pet, item_cache)
        if item_id is None:
            skipped.append(("no item for pet name", pid, pet))
            continue
        best.setdefault((pid, item_id), (ts, pid, item_id, pet, origin))

    written, present = [], 0
    for (pid, item_id), (ts, _, _, pet, origin) in sorted(best.items(), key=lambda kv: kv[1][0]):
        # Same item, or the same pet under another variant id (Baby chinchompa
        # colours, metamorph forms): either way the player already has it.
        has = session.query(PlayerPet).filter(
            PlayerPet.player_id == pid,
            (PlayerPet.item_id == item_id) | (PlayerPet.pet_name == pet)).first()
        if has is not None:
            present += 1
            continue
        uid = "ccbf:" + hashlib.sha256(f"{pid}|{item_id}".encode()).hexdigest()
        session.add(PlayerPet(player_id=pid, item_id=item_id, pet_name=pet,
                              unique_id=uid, date_added=ts))
        written.append({"player_id": pid, "item_id": item_id, "pet": pet,
                        "date_added": ts.isoformat(), "from": origin})
    session.flush()
    if args.apply:
        session.commit()
    else:
        session.rollback()

    print(f"candidates: {len(candidates)} lines -> {len(best)} distinct player+pet")
    print(f"already recorded: {present}")
    print(f"{'WROTE' if args.apply else 'would write'}: {len(written)}")
    for w in written:
        print(f"  {w['date_added']}  player {w['player_id']:>8}  {w['pet']} (item {w['item_id']})  [{w['from']}]")
    if skipped:
        print(f"skipped: {len(skipped)}")
        for s in skipped:
            print("  ", s)

    out = os.path.join(os.path.dirname(__file__), "..", "logs",
                       f"backfill_lost_clan_pets_{'apply' if args.apply else 'dryrun'}_"
                       f"{datetime.utcnow():%Y%m%dT%H%M%SZ}.json")
    try:
        with open(out, "w") as f:
            json.dump({"apply": args.apply, "written": written,
                       "skipped": [list(map(str, s)) for s in skipped]}, f, indent=2)
        print(f"audit: {out}")
    except Exception as e:
        print(f"audit not written: {e}")


if __name__ == "__main__":
    main()
