"""Re-queue pet submissions that never reached the events engine.

Until 2026-08-03 ``data.submissions.pet.pet_processor`` gated its
``queue_submission`` call on ``is_new_pet``, so a DUPLICATE pet (one the
player already had a ``player_pets`` row for) was processed, notified and
point-awarded — but no envelope was ever pushed onto ``events:submissions``.
Any event task it should have credited silently missed it.

The producer no longer gates on ``is_new_pet``; it ships the flag instead and
the matcher decides per task type (``item_collection`` accepts duplicates —
a "get 5 of these 4 items" tile is unsatisfiable without them;
``pet_collection`` and ``loot_sweep`` refuse them). This script rebuilds the
lost envelopes from the ``notification_queue`` rows those submissions DID
write, and pushes them back onto the queue for the consumer to evaluate
normally.

Replays are safe to repeat: the ledger's
``uq_web_evt_completion_src (task_id, team_id, submission_guid)`` makes a
second run of the same guid a no-op insert.

The envelope keeps its ORIGINAL timestamp, so the engine's window / joined_at
/ schedule checks judge it exactly as they would have at the time — a replay
cannot smuggle credit into an event that had already closed.

Usage:
    venv/bin/python -m scripts.replay_pet_event_submissions            # dry run
    venv/bin/python -m scripts.replay_pet_event_submissions --apply
    venv/bin/python -m scripts.replay_pet_event_submissions --guid <guid>
    venv/bin/python -m scripts.replay_pet_event_submissions --since 2026-08-01
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (  # noqa: E402
    Event, EventCompletion, EventTeam, EventTeamMember, NotificationQueue,
    Player, session,
)
from services.event_engine import QUEUE_KEY  # noqa: E402


def _pet_rows(guid=None, since=None):
    """Distinct pet submissions whose envelope was suppressed (one row per
    guid — the processor writes one notification per group)."""
    query = (session.query(NotificationQueue)
             .filter(NotificationQueue.notification_type == "pet"))
    if since:
        query = query.filter(NotificationQueue.created_at >= since)
    by_guid = {}
    for row in query.order_by(NotificationQueue.id).all():
        try:
            data = json.loads(row.data)
        except (TypeError, ValueError):
            continue
        row_guid = data.get("guid")
        if not row_guid or (guid and row_guid != guid):
            continue
        # Only the suppressed ones. `is_new_pet` is a real bool here (the
        # processor writes it from the DB lookup), unlike the payload's
        # `duplicate`, which arrives as the string "true"/"false".
        if data.get("is_new_pet") not in (False, "false"):
            continue
        by_guid.setdefault(row_guid, (row, data))
    return list(by_guid.values())


def _memberships(player_id, submitted_at):
    """(event, team_id) pairs whose window admits this submission — the same
    conditions handle_envelope applies, evaluated here only to report what a
    replay is expected to reach."""
    rows = (session.query(Event, EventTeamMember.team_id,
                          EventTeamMember.joined_at)
            .join(EventTeam, EventTeam.event_id == Event.id)
            .join(EventTeamMember, EventTeamMember.team_id == EventTeam.id)
            .filter(EventTeamMember.player_id == player_id)
            .all())
    live = []
    for event, team_id, joined_at in rows:
        if event.status != "active":
            continue
        if joined_at is not None and submitted_at < joined_at:
            continue
        if event.starts_at is not None and submitted_at < event.starts_at:
            continue
        if event.ends_at is not None and submitted_at > event.ends_at:
            continue
        live.append((event, team_id))
    return live


def _already_credited(guid):
    return (session.query(EventCompletion)
            .filter(EventCompletion.submission_guid == guid)
            .first() is not None)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="push the envelopes (default: dry run)")
    ap.add_argument("--guid", help="replay one submission guid")
    ap.add_argument("--since", help="only rows created on/after YYYY-MM-DD")
    args = ap.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    rows = _pet_rows(guid=args.guid, since=since)
    if not rows:
        print("No suppressed pet submissions found.")
        return

    conn = None
    if args.apply:
        from utils.redis import redis_client
        conn = getattr(redis_client, "client", None)
        if conn is None:
            print("ERROR: no Redis connection — cannot push.")
            sys.exit(1)

    pushed = skipped = 0
    for row, data in rows:
        guid = data["guid"]
        player_id = row.player_id
        player = session.query(Player).filter(
            Player.player_id == player_id).first()
        player_name = data.get("player_name") or (
            player.player_name if player else None)
        pet_name = data.get("pet_name")
        submitted_at = row.created_at

        live = _memberships(player_id, submitted_at)
        credited = _already_credited(guid)
        tag = ", ".join(f"event {e.id} ({e.name}) team {t}"
                        for e, t in live) or "no live event membership"
        state = "ALREADY CREDITED" if credited else tag

        print(f"{'PUSH' if not credited and live else 'SKIP'}  {pet_name:<22} "
              f"{player_name:<16} {submitted_at}  {state}")

        if credited or not live:
            skipped += 1
            continue
        envelope = {
            "v": 1,
            "kind": "pet",
            "guid": guid,
            "player_id": int(player_id),
            "ts": int(submitted_at.timestamp()),
            # These all came from the plugin (the manual paths stamp
            # intake_source); confirm_non_api events must auto-apply them.
            "used_api": True,
            "player_name": player_name,
            "data": {
                "pet_name": pet_name,
                "item_id": data.get("item_id"),
                "item_name": pet_name,
                "npc_name": data.get("npc_name"),
                "killcount": data.get("killcount"),
                "source_id": None,
                "is_new_pet": False,
            },
        }
        if args.apply:
            conn.lpush(QUEUE_KEY, json.dumps(envelope, default=str))
        else:
            print(f"      {json.dumps(envelope)}")
        pushed += 1

    verb = "Pushed" if args.apply else "Would push"
    print(f"\n{verb} {pushed} envelope(s); skipped {skipped}.")
    if not args.apply and pushed:
        print("Re-run with --apply to push.")


if __name__ == "__main__":
    main()
