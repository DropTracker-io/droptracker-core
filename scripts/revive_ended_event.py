"""Put a prematurely ended event back to draft so its schedule can run.

Built for the 2026-09-01 October Bingo incident (event 58): a leader pressed
Activate on a draft scheduled for Oct 1 expecting it to open sign-ups, the
event went live a month early, and ending it to undo the mistake left it
``past`` — a state the lifecycle service refuses to reactivate. The
activate-button fix that landed alongside stops the next leader falling in;
this script repairs an event that already did.

Only an event with NO scoring history is eligible (no completions, points,
progress, coin ledger or bingo completions): a revive rewinds the lifecycle,
it does not erase results. What it rewinds:

* ``status`` -> draft, ``activated_at``/``ended_at`` -> NULL (the sweep will
  activate it again at ``starts_at``; the tier rate-limit slot the false
  start consumed is released because stamps count ``activated_at``);
* team Discord rows the end marked ``delete_pending`` go back to ``synced``
  with the grace deadline cleared, so the reconciler keeps the roles and
  channels instead of tearing them down 48h after the false end;
* the Redis ended-tombstone is cleared so a stale matcher snapshot can't
  keep treating it as over;
* one ``event.revive`` audit row records the repair.

The Discord "event started"/"event ended" posts already sent cannot be
recalled from here.

Usage:
    venv/bin/python scripts/revive_ended_event.py --event 58            # dry run
    venv/bin/python scripts/revive_ended_event.py --event 58 --apply
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (  # noqa: E402
    AuditLog, Event, EventBingoCell, EventBingoCompletion, EventCompletion,
    EventPlayerPoints, EventProgress, EventTeamDiscord, session,
)


def _history(s, event_id: int) -> dict:
    counts = {
        "completions": s.query(EventCompletion).filter(
            EventCompletion.event_id == event_id).count(),
        "player_points": s.query(EventPlayerPoints).filter(
            EventPlayerPoints.event_id == event_id).count(),
        "progress": s.query(EventProgress).filter(
            EventProgress.event_id == event_id).count(),
        "bingo_completions": s.query(EventBingoCompletion).join(
            EventBingoCell, EventBingoCell.id == EventBingoCompletion.cell_id
        ).filter(EventBingoCell.event_id == event_id).count(),
    }
    try:
        from db.models import EventCoinLedger

        counts["coin_ledger"] = s.query(EventCoinLedger).filter(
            EventCoinLedger.event_id == event_id).count()
    except ImportError:
        pass
    return counts


def _clear_tombstone(event_id: int) -> str:
    try:
        from services import event_engine
        from utils.redis import redis_client

        conn = getattr(redis_client, "client", None)
        if conn is None:
            return "redis unavailable"
        conn.srem(event_engine.ACTIVE_EVENTS_KEY, int(event_id))
        event_engine.clear_ended_tombstone(conn, event_id)
        return "cleared"
    except Exception as exc:  # best-effort, like the lifecycle service
        return f"failed: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--event", type=int, required=True, help="event id to revive")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--actor", type=int, default=None,
                    help="user id to record on the audit row (default: none)")
    args = ap.parse_args()

    s = session
    ev = s.query(Event).filter(Event.id == args.event).first()
    if ev is None:
        print(f"No event {args.event}.")
        return 1
    print(f"Event {ev.id} '{ev.name}' group={ev.group_id} kind={ev.kind} status={ev.status}")
    print(f"  starts_at={ev.starts_at} ends_at={ev.ends_at} "
          f"activated_at={ev.activated_at} ended_at={ev.ended_at}")
    if ev.status == "draft":
        print("Already a draft — nothing to do.")
        return 0
    if ev.starts_at is None or ev.starts_at <= datetime.now():
        print("Refusing: the event has no future starts_at, so reviving it as a draft "
              "would just have the sweep re-activate it immediately.")
        return 1
    history = _history(s, ev.id)
    print(f"  scoring history: {history}")
    if any(history.values()):
        print("Refusing: the event has scoring history; a revive rewinds the "
              "lifecycle, it does not erase results.")
        return 1

    discord_rows = (s.query(EventTeamDiscord)
                    .filter(EventTeamDiscord.event_id == ev.id,
                            EventTeamDiscord.sync_status == "delete_pending")
                    .all())
    print(f"  team Discord rows pending deletion: {len(discord_rows)}"
          + (f" (delete_after={discord_rows[0].delete_after})" if discord_rows else ""))

    if not args.apply:
        print("Dry run — re-run with --apply to revive.")
        return 0

    before = {"status": ev.status, "activated_at": ev.activated_at,
              "ended_at": ev.ended_at}
    ev.status = "draft"
    ev.activated_at = None
    ev.ended_at = None
    for row in discord_rows:
        row.sync_status = "synced"
        row.delete_after = None
    s.add(AuditLog(
        actor_user_id=args.actor,
        group_id=ev.group_id,
        action="event.revive",
        target=f"web_events.{ev.id}",
        before=json.dumps(before, default=str),
        after=json.dumps({"status": "draft", "activated_at": None, "ended_at": None,
                          "team_discord_rows_restored": len(discord_rows)}),
    ))
    s.commit()
    print(f"Revived event {ev.id} as a draft; restored {len(discord_rows)} team Discord rows.")
    print(f"  Redis gate/tombstone: {_clear_tombstone(ev.id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
