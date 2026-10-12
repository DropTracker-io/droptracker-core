"""Re-score drops an event refused only because of when they arrived.

Built for event 129 (2026-10-11): it was activated at 01:09 while its start
said 14:00, and at 02:45 an admin moved the start back to 02:00. The engine
judges a submission once, on arrival, so every drop from 02:00 to 02:45 had
already been refused as "before the start", and ~4.6M GP never counted.
(From now on the consumer parks such envelopes and replays them itself when
a start moves earlier: ``event_engine.park_early`` /
``replay_early_if_moved``. This script repairs what happened before that.)

It rebuilds the drop envelopes from the ``drops`` table for the event's
current members inside ``[--since, --until]`` that have no ledger row on the
event, pins each one to the event (``only_event_id``) and pushes it onto the
events queue with its ORIGINAL timestamp, so the consumer judges it exactly
as it would have on arrival against today's window, joined_at and policy.
Replays are idempotent: ``uq_web_evt_completion_src (task_id, team_id,
submission_guid)`` turns a second run of the same guid into a no-op.

Clan-chat broadcast rows (``source='clan_chat'``) are skipped: they never feed
events, by design (data/submissions/clan_broadcast.py). Only drops are
rebuilt — the party list isn't stored, so group-content race rules (BotW
``party``) would judge a replayed kill as solo.

``--silent`` applies in THIS process instead of queueing: the same
``handle_envelope`` the consumer runs, with every notification path (Discord
``notification_queue`` rows, lead changes, in-game inbox fan-out) stubbed out.
Scores, progress, contribution points and live SSE frames update; nobody is
told about points that were earned hours ago.

Usage:
    venv/bin/python scripts/replay_event_drops.py --event 129 \\
        --since "2026-10-11 02:00" --until "2026-10-11 02:45"          # dry run
    venv/bin/python scripts/replay_event_drops.py --event 129 \\
        --since "2026-10-11 02:00" --until "2026-10-11 02:45" --apply  # via the queue
    venv/bin/python scripts/replay_event_drops.py --event 129 \\
        --since "2026-10-11 02:00" --until "2026-10-11 02:45" --apply --silent
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import and_, exists, or_  # noqa: E402

from db.models import (  # noqa: E402
    Drop, Event, EventCompletion, EventTeamMember, ItemList, NpcList, Player,
    session,
)
from services.event_engine import QUEUE_KEY  # noqa: E402


def _rows(event_id: int, since: datetime, until: datetime):
    credited = exists().where(and_(EventCompletion.event_id == event_id,
                                   EventCompletion.submission_guid == Drop.unique_id))
    return (
        session.query(Drop, Player.player_name, ItemList.item_name, NpcList.npc_name)
        .join(EventTeamMember, and_(EventTeamMember.player_id == Drop.player_id,
                                    EventTeamMember.event_id == event_id))
        .join(Player, Player.player_id == Drop.player_id)
        .outerjoin(ItemList, ItemList.item_id == Drop.item_id)
        .outerjoin(NpcList, NpcList.npc_id == Drop.npc_id)
        .filter(Drop.date_added >= since, Drop.date_added <= until,
                Drop.hidden.is_(False),
                Drop.unique_id.isnot(None),
                or_(Drop.source.is_(None), Drop.source != "clan_chat"),
                ~credited)
        .order_by(Drop.date_added.asc(), Drop.drop_id.asc())
        .all()
    )


def _envelope(event_id: int, drop, player_name, item_name, npc_name) -> dict:
    quantity = int(drop.quantity or 1)
    value = int(drop.value or 0)
    return {
        "v": 1,
        "kind": "drop",
        "guid": str(drop.unique_id),
        "player_id": int(drop.player_id),
        "player_name": player_name,
        # The engine reads ts with datetime.fromtimestamp (server-local, UTC
        # here), the same clock date_added was written on.
        "ts": int(drop.date_added.timestamp()),
        # "Came from the plugin" for submission policies: only manual intake
        # is non-plugin (common.envelope_from_plugin).
        "used_api": (drop.source or "") != "manual",
        "only_event_id": int(event_id),
        "data": {
            "item_id": drop.item_id,
            "item_name": item_name,
            "npc_id": drop.npc_id,
            "npc_name": npc_name,
            "value": value,
            "quantity": quantity,
            "total_value": value * quantity,
            "kill_count": drop.kill_count,
            "image_url": drop.image_url,
            "source_id": drop.drop_id,
            "party": [],
            "party_size": None,
            "replayed": "scripts/replay_event_drops.py",
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event", type=int, required=True)
    ap.add_argument("--since", required=True, help="YYYY-MM-DD HH:MM[:SS] (server time)")
    ap.add_argument("--until", required=True, help="YYYY-MM-DD HH:MM[:SS] (server time)")
    ap.add_argument("--apply", action="store_true", help="push to the queue (default: dry run)")
    ap.add_argument("--silent", action="store_true",
                    help="with --apply: score in-process with notifications suppressed")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since)
    until = datetime.fromisoformat(args.until)
    ev = session.query(Event).filter(Event.id == args.event).first()
    if ev is None:
        print(f"No event {args.event}.")
        return 1
    if ev.status != "active":
        print(f"Event {ev.id} is {ev.status}; only a live event scores replays.")
        return 1
    rows = _rows(ev.id, since, until)
    per_player: dict = {}
    for drop, name, _item, _npc in rows:
        n, gp = per_player.get(name, (0, 0))
        per_player[name] = (n + 1, gp + int(drop.value or 0) * int(drop.quantity or 1))
    print(f"Event {ev.id} {ev.name!r}: {len(rows)} uncredited drop(s) in "
          f"[{since}, {until}], {sum(gp for _, gp in per_player.values()):,} GP")
    for name, (n, gp) in sorted(per_player.items(), key=lambda kv: -kv[1][1]):
        print(f"  {name!r:<24} {n:>4} drop(s) {gp:>12,} GP")
    if not args.apply:
        print("Dry run — nothing queued. Re-run with --apply.")
        return 0
    if not rows:
        return 0

    from utils.redis import redis_client

    conn = redis_client.client
    if args.silent:
        return _apply_silently(conn, ev.id, rows)
    payloads = [json.dumps(_envelope(ev.id, *row), default=str) for row in rows]
    # Right (consuming) end, newest pushed first, so they apply oldest-first
    # and ahead of live traffic — the same order replay_prestart uses.
    conn.rpush(QUEUE_KEY, *reversed(payloads))
    print(f"Queued {len(payloads)} envelope(s) on {QUEUE_KEY}.")
    return 0


def _apply_silently(conn, event_id: int, rows) -> int:
    """Score ``rows`` through the engine in-process, one transaction per
    envelope (the consumer's unit), with notifications stubbed out."""
    from services import event_engine
    from services import plugin_notifications

    event_engine._enqueue_notification = lambda *a, **k: None
    plugin_notifications.fan_out_event_notification = lambda *a, **k: 0

    state = event_engine.load_matcher_state(session)
    if event_id not in state.events:
        print(f"Event {event_id} isn't in the matcher state (not live?).")
        return 1
    scored = skipped = 0
    for row in rows:
        envelope = _envelope(event_id, *row)
        staged = event_engine.StagedWrites()
        try:
            results = event_engine.handle_envelope(session, conn, state, envelope,
                                                   staged=staged)
            session.commit()
            staged.flush(conn)
        except Exception as e:
            session.rollback()
            print(f"  failed {envelope['guid']}: {e}")
            continue
        if results:
            scored += 1
        else:
            skipped += 1
    print(f"Scored {scored} drop(s); {skipped} matched nothing. No notifications sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
