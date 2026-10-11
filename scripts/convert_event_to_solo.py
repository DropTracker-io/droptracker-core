"""Convert a running event to free-for-all (formation_mode "solo").

Built for event 129 ("Fractured Archives Money Grab", 2026-10-11): a clan meant
every player to race for the most GP, but "Sign up all members" put all 272
of them on ONE team, so GP pooled clan-wide, every pooled 1M was a completion
announced to everyone, and the placement points / winner-takes-pot payout
had nobody to rank.

The same conversion the event settings route runs when an admin switches
the formation mode to free-for-all (``services.event_signup
.split_into_solo_teams``): every member of a shared team gets a team of their
own (named after their RSN, keeping their credit cutoff), takes their own
ledger rows / effort / buy-ins with them, and every task re-folds from the
ledger, so each player's progress, laps and score are what they would have
been entered alone. The emptied teams are deleted. No lead-change message is
posted. Ledger rows with no player can't be attributed and are reported.

Usage:
    venv/bin/python scripts/convert_event_to_solo.py --event 129            # dry run
    venv/bin/python scripts/convert_event_to_solo.py --event 129 --apply
    venv/bin/python scripts/convert_event_to_solo.py --event 129 --retarget 3010:1000000:1 --apply

The dry run performs the whole conversion inside a transaction and rolls it
back, so its numbers are the real outcome.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (  # noqa: E402
    AuditLog, Event, EventTeam, EventTeamMember, session,
)


def _standings(s, event_id: int, limit: int = 15) -> list:
    rows = (s.query(EventTeam.id, EventTeam.name, EventTeam.score)
            .filter(EventTeam.event_id == event_id)
            .order_by(EventTeam.score.desc(), EventTeam.id.asc())
            .all())
    return rows[:limit], len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--event", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run)")
    ap.add_argument("--retarget", metavar="TASK:GOAL:POINTS",
                    help="also set one task's goal/points after the split "
                         "(e.g. 3010:1000000:1), re-folded silently like a live edit")
    args = ap.parse_args()
    retarget = None
    if args.retarget:
        try:
            tid, goal, pts = (int(x) for x in args.retarget.split(":"))
        except ValueError:
            print("--retarget must be TASK:GOAL:POINTS (integers).")
            return 1
        retarget = (tid, goal, pts)

    from services.event_signup import (SignupError, solo_blocked_reason,
                                       split_into_solo_teams)

    s = session
    ev = s.query(Event).filter(Event.id == args.event).first()
    if ev is None:
        print(f"No event {args.event}.")
        return 1
    problem = solo_blocked_reason(ev.kind, ev.mode)
    if problem:
        print(f"Refused: {problem}")
        return 1
    print(f"Event {ev.id} {ev.name!r}: status={ev.status} kind={ev.kind} "
          f"formation={ev.formation_mode}")
    before, n_before = _standings(s, ev.id)
    members = (s.query(EventTeamMember)
               .filter(EventTeamMember.event_id == ev.id).count())
    print(f"Before: {n_before} team(s), {members} member(s)")
    for tid, name, score in before[:5]:
        print(f"  team {tid:>6} {name!r:<28} score={score:g}")

    previous_mode = ev.formation_mode
    try:
        ev.formation_mode = "solo"
        summary = split_into_solo_teams(s, ev)
    except SignupError as e:
        s.rollback()
        print(f"Refused: {e.detail}")
        return 1
    print("Split:", json.dumps(summary))
    from db.models import EventProgress, EventTask

    retarget_audit = None
    if retarget:
        from services.event_engine import recompute_task_rollups

        tid, goal, pts = retarget
        task = (s.query(EventTask)
                .filter(EventTask.id == tid, EventTask.event_id == ev.id).first())
        if task is None:
            s.rollback()
            print(f"No task {tid} on event {ev.id}.")
            return 1
        old_task = {"type": task.type, "target_value": task.target_value,
                    "config": task.config}
        old_points = task.points
        task.target_value, task.points = goal, pts
        s.flush()
        result = recompute_task_rollups(s, ev, task, old_points=old_points,
                                        old_task=old_task, announce_lead=False)
        changed = {k: v for k, v in result["teams"].items() if v.get("score_delta")}
        print(f"Retarget task {tid}: {old_task['target_value']}/{old_points} pt -> "
              f"{goal}/{pts} pt; {len(changed)} team score(s) moved")
        retarget_audit = {
            "task_id": tid,
            "from": {"target_value": old_task["target_value"], "points": old_points},
            "to": {"target_value": goal, "points": pts},
        }
    after, n_after = _standings(s, ev.id)
    print(f"After: {n_after} team(s)")
    for tid, name, score in after:
        print(f"  team {tid:>6} {name!r:<28} score={score:g}")

    for task in s.query(EventTask).filter(EventTask.event_id == ev.id).all():
        top = (s.query(EventTeam.name, EventProgress.progress)
               .join(EventTeam, EventTeam.id == EventProgress.team_id)
               .filter(EventProgress.task_id == task.id)
               .order_by(EventProgress.progress.desc())
               .limit(10).all())
        print(f"Task {task.id} {task.label!r} (goal {task.target_value}, "
              f"{task.points} pt): top progress")
        for name, progress in top:
            print(f"  {name!r:<28} {progress:,.0f}")

    if not args.apply:
        s.rollback()
        print("Dry run — rolled back. Re-run with --apply to commit.")
        return 0

    s.add(AuditLog(
        actor_user_id=None, group_id=ev.group_id, event_id=ev.id,
        action="event.settings.update", target=f"web_events.{ev.id}",
        after=json.dumps({
            "formation_mode": {"from": previous_mode, "to": "solo"},
            "solo_split": summary,
            "via": "scripts/convert_event_to_solo.py",
        }),
    ))
    if retarget_audit:
        s.add(AuditLog(
            actor_user_id=None, group_id=ev.group_id, event_id=ev.id,
            action="event.task.update",
            target=f"web_event_tasks.{retarget_audit['task_id']}",
            before=json.dumps(retarget_audit["from"]),
            after=json.dumps({**retarget_audit["to"], "retro": "recompute",
                              "via": "scripts/convert_event_to_solo.py"}),
        ))
    s.commit()
    try:
        from services.event_engine import publish_event_admin_bump

        publish_event_admin_bump(ev.id)
    except Exception as e:  # the consumer re-reads within 30s anyway
        print(f"(matcher bump failed: {e})")
    print("Committed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
