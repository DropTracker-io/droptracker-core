"""Delete orphaned bingo-task clones left behind by the pre-fix board designer.

Until the board-save fix in ``web_api/routes/event_admin.py put_bingo_board``
(2026-07-18), re-saving a bingo board cloned library picks / inline tasks into
fresh ``bingo_auto`` rows instead of binding the identical task row that
already existed — leaving the twin orphaned (no ``web_event_bingo_cells`` row)
in the event's task list. The engine used to evaluate BOTH twins per
submission (the double-credit pollution revoked by
``scripts/revoke_clog_echo_completions.py``); it now skips non-cell-bound
tasks on bingo events entirely, so the orphan rows are pure dead weight
cluttering the admin task list (e.g. event 9's 41/43/53/54/56, twinned with
45/61/58/60/59).

A task is deleted only when ALL of:

* it is not bound by any ``web_event_bingo_cells`` row;
* a cell-bound task in the SAME event has identical content — (type, label,
  target, target_value, points, requires_confirmation, config) under the same
  canonicalization the board save uses (case/whitespace-insensitive
  label+target, config key-order-insensitive and ignoring the ``bingo_auto``
  marker);
* it carries no live or undecided credit: no applied (auto/confirmed/manual)
  or pending ledger rows, and no progress rollup with progress > 0 or
  completed.

Deleted with it (all FK ``web_event_tasks.id`` with no cascade): its zeroed
``web_event_progress`` rows, its non-applied ``web_event_completions`` rows
(revoked/rejected history), and any stray ``web_event_player_points`` rows;
nullable board-tile/position pointers are unbound for parity with the
``delete_task`` endpoint. Twinless orphans (e.g. event 9 task 38 — possibly
intentional) are never touched, nor is anything with live credit — run
``revoke_clog_echo_completions.py`` first if phantom credit remains.

Scope defaults to ACTIVE events; ``--events`` targets named ids instead
(including past events). Bingo events only — ``board_game`` events keep
off-board tasks as their roll pool and are always skipped. Idempotent:
deleted tasks simply stop matching.

Usage:
    venv/bin/python scripts/cleanup_orphan_bingo_task_clones.py             # dry run
    venv/bin/python scripts/cleanup_orphan_bingo_task_clones.py --apply
    venv/bin/python scripts/cleanup_orphan_bingo_task_clones.py --events 9 --apply
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func  # noqa: E402

from db.models import (  # noqa: E402
    Event, EventBingoCell, EventCompletion, EventProgress, EventTask, session,
)
from db.models.events import EventPlayerPoints  # noqa: E402

APPLIED = ("auto", "confirmed", "manual")
_BINGO_AUTO_KEY = "bingo_auto"


def _task_identity(task: EventTask) -> tuple:
    """Content identity of a task row.

    Mirrors ``_task_identity`` in web_api/routes/event_admin.py (the board
    save's reuse key) — keep the semantics in sync. Only the DB-row shape is
    handled here (``config`` is Text or NULL)."""
    canonical_config = None
    if task.config:
        try:
            parsed = json.loads(task.config)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            parsed.pop(_BINGO_AUTO_KEY, None)
            canonical_config = json.dumps(parsed, sort_keys=True) if parsed else None
        else:
            canonical_config = str(task.config)
    return (
        task.type,
        (task.label or "").strip()[:255].lower(),
        ((task.target or "").strip().lower() or None),
        task.target_value,
        int(task.points or 0),
        bool(task.requires_confirmation),
        canonical_config,
    )


def _target_events(event_ids):
    query = session.query(Event)
    if event_ids:
        query = query.filter(Event.id.in_(event_ids))
    else:
        query = query.filter(Event.status == "active")
    return query.all()


def _blockers(task):
    """Reasons this clone must NOT be deleted, plus its ledger status counts."""
    status_counts = dict(
        session.query(EventCompletion.status, func.count())
        .filter(EventCompletion.task_id == task.id)
        .group_by(EventCompletion.status).all()
    )
    blockers = []
    applied = {k: v for k, v in status_counts.items() if k in APPLIED}
    if applied:
        blockers.append(f"applied ledger rows {applied} — revoke first")
    if status_counts.get("pending"):
        blockers.append(
            f"{status_counts['pending']} pending ledger rows — adjudicate first")
    live = [
        (p.team_id, int(p.progress or 0), bool(p.completed))
        for p in session.query(EventProgress)
        .filter(EventProgress.task_id == task.id).all()
        if int(p.progress or 0) != 0 or p.completed
    ]
    if live:
        blockers.append(f"non-zero/completed progress rollups {live}")
    return blockers, status_counts


def _clone_candidates(event):
    """(clone task, its cell-bound twin, blockers, ledger counts) tuples."""
    bound_ids = {
        cell.task_id
        for cell in session.query(EventBingoCell)
        .filter(EventBingoCell.event_id == event.id,
                EventBingoCell.task_id.isnot(None)).all()
    }
    tasks = (session.query(EventTask)
             .filter(EventTask.event_id == event.id)
             .order_by(EventTask.id).all())
    bound_by_identity = {}
    for t in tasks:
        if t.id in bound_ids:
            bound_by_identity.setdefault(_task_identity(t), []).append(t)
    out = []
    for t in tasks:
        if t.id in bound_ids:
            continue
        twins = bound_by_identity.get(_task_identity(t))
        if not twins:
            continue  # twinless orphan — possibly intentional, leave alone
        blockers, status_counts = _blockers(t)
        out.append((t, twins[0], blockers, status_counts))
    return out


def _delete_clone(task) -> tuple[int, int, int] | None:
    """Delete the task and its FK children; returns row counts removed, or
    None when the delete was refused.

    A concurrent designer save on a not-yet-started event could bind this row
    between selection and delete (the fixed save reuses identical tasks), so
    re-check the cells right before deleting."""
    if (session.query(EventBingoCell.id)
            .filter(EventBingoCell.task_id == task.id).first()):
        return None
    n_ledger = (session.query(EventCompletion)
                .filter(EventCompletion.task_id == task.id)
                .delete(synchronize_session=False))
    n_progress = (session.query(EventProgress)
                  .filter(EventProgress.task_id == task.id)
                  .delete(synchronize_session=False))
    n_points = (session.query(EventPlayerPoints)
                .filter(EventPlayerPoints.task_id == task.id)
                .delete(synchronize_session=False))
    # delete_task parity: nullable board-game pointers. Bingo events have
    # none, but unbinding is free and keeps the delete safe regardless.
    from db import EventBoardPosition, EventBoardTile

    (session.query(EventBoardTile)
     .filter(EventBoardTile.task_id == task.id)
     .update({EventBoardTile.task_id: None}, synchronize_session=False))
    (session.query(EventBoardPosition)
     .filter(EventBoardPosition.current_task_id == task.id)
     .update({EventBoardPosition.current_task_id: None},
             synchronize_session=False))
    session.delete(task)
    session.flush()
    return (n_ledger, n_progress, n_points)


def main(apply, event_ids):
    events = _target_events(event_ids)
    if not events:
        print("No matching events.")
        return
    deleted = skipped = 0
    ledger_total = progress_total = points_total = 0
    for event in sorted(events, key=lambda e: e.id):
        if not event.has_bingo or (getattr(event, "kind", None) or "") == "board_game":
            if event_ids:
                print(f"\nEvent {event.id} — {event.name}: skipped "
                      "(not a bingo event)")
            continue
        candidates = _clone_candidates(event)
        if not candidates:
            if event_ids:
                print(f"\nEvent {event.id} — {event.name}: no orphan clones.")
            continue
        print(f"\nEvent {event.id} — {event.name} [{event.status}]")
        for task, twin, blockers, status_counts in candidates:
            print(f"  task {task.id} [{task.type}] {task.label!r} "
                  f"— unbound clone of cell-bound task {twin.id}")
            n_progress = (session.query(func.count(EventProgress.id))
                          .filter(EventProgress.task_id == task.id).scalar())
            n_points = (session.query(func.count(EventPlayerPoints.id))
                        .filter(EventPlayerPoints.task_id == task.id).scalar())
            print(f"    ledger: {status_counts or 'none'} | "
                  f"progress rows: {n_progress} | player-point rows: {n_points}")
            if blockers:
                skipped += 1
                for reason in blockers:
                    print(f"    -> SKIP: {reason}")
                continue
            n_ledger = sum(status_counts.values())
            if apply:
                removed = _delete_clone(task)
                if removed is None:
                    skipped += 1
                    print(f"    -> SKIP: task {task.id} became cell-bound "
                          "mid-run — left alone")
                    continue
                n_ledger, n_progress, n_points = removed
                print(f"    -> DELETED (with {n_ledger} ledger, {n_progress} "
                      f"progress, {n_points} player-point rows)")
            else:
                print("    -> would DELETE (with its ledger/progress/"
                      "player-point rows above)")
            deleted += 1
            ledger_total += n_ledger
            progress_total += n_progress
            points_total += n_points
    if apply:
        session.commit()
    verb = "deleted" if apply else "to delete"
    print(f"\n{'APPLIED' if apply else 'DRY RUN'}: {deleted} clone tasks "
          f"{verb} ({ledger_total} ledger, {progress_total} progress, "
          f"{points_total} player-point rows), {skipped} skipped.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    ids = []
    for arg in sys.argv[1:]:
        if arg.startswith("--events="):
            ids = [int(x) for x in arg.split("=", 1)[1].split(",") if x.strip()]
        elif arg == "--events":
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv):
                ids = [int(x) for x in sys.argv[idx + 1].split(",") if x.strip()]
    try:
        main(apply, ids)
    except Exception:
        session.rollback()
        raise
