"""Backfill web_event_player_points for tasks completed BEFORE the
contribution-points feature shipped (2026-07-16).

For every completed (task, team) EventProgress row with no player-points rows
yet, split the task's points across contributors by net applied-ledger share —
counting only rows recorded up to the completion time (+2s slack for the
same-transaction completing row), so the pre-fix post-completion pollution
never inflates anyone's share.

Usage:
    venv/bin/python scripts/backfill_event_player_points.py         # dry run
    venv/bin/python scripts/backfill_event_player_points.py --apply
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (  # noqa: E402
    EventCompletion, EventPlayerPoints, EventProgress, EventTask, session,
)

APPLIED = ("auto", "confirmed", "manual")


def main(apply: bool) -> None:
    completed = (session.query(EventProgress)
                 .filter(EventProgress.completed.is_(True))
                 .all())
    tasks = {t.id: t for t in session.query(EventTask).all()}
    seeded = {
        (task_id, team_id)
        for task_id, team_id in session.query(
            EventPlayerPoints.task_id, EventPlayerPoints.team_id).distinct()
    }

    written = skipped = 0
    for prog in completed:
        key = (prog.task_id, prog.team_id)
        task = tasks.get(prog.task_id)
        points = int(getattr(task, "points", 0) or 0)
        if key in seeded or task is None or not points:
            skipped += 1
            continue
        cutoff = (prog.completed_at + timedelta(seconds=2)) if prog.completed_at else None
        q = (session.query(EventCompletion)
             .filter(EventCompletion.task_id == prog.task_id,
                     EventCompletion.team_id == prog.team_id,
                     EventCompletion.status.in_(APPLIED)))
        if cutoff is not None:
            q = q.filter(EventCompletion.created_at <= cutoff)
        totals: dict[int, int] = {}
        for row in q.all():
            if (row.source_type or "") == "bonus" or row.player_id is None:
                continue
            totals[row.player_id] = totals.get(row.player_id, 0) + max(int(row.quantity or 1), 1)
        total = sum(totals.values())
        if total <= 0:
            skipped += 1
            continue
        for pid, qty in totals.items():
            share = round(points * qty / total, 2)
            if share <= 0:
                continue
            print(f"event {prog.event_id} task {prog.task_id} team {prog.team_id} "
                  f"player {pid}: {qty}/{total} of {points} -> {share}")
            if apply:
                session.add(EventPlayerPoints(
                    event_id=prog.event_id, task_id=prog.task_id,
                    team_id=prog.team_id, player_id=pid, points=share))
        written += 1

    if apply:
        session.commit()
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {written} completed (task, team) "
          f"rollups backfilled, {skipped} skipped (already seeded / zero points / no contributors)")


if __name__ == "__main__":
    main("--apply" in sys.argv)
