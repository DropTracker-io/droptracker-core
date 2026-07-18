"""Repair event ledgers polluted by clog-echo double credits and orphan tasks.

Two related pollutions, both fixed at the source on 2026-07-18 but leaving
history behind:

1. **Clog echoes** — one physical drop used to be credited twice on
   item_collection tasks: once from the drop submission and once from the
   collection-log submission it unlocked (separate guids, seconds apart).
   ``services.event_engine._dedupe_clog_echo`` now blocks these at insert;
   this script revokes the historical clog rows whose paired drop row exists
   within the same window, letting ``revoke_ledger_row`` re-fold progress,
   scores, cells, bonuses and contribution shares.

2. **Orphan bingo tasks** — duplicate task rows not bound to any board cell
   (board saves cloned tasks instead of linking them). The matcher no longer
   evaluates them for bingo events; this revokes whatever they already
   credited (e.g. a phantom task completion worth team points).

Scope defaults to ACTIVE events only — past events' final standings are
records, not live scores, and are left untouched unless --events names them.

Usage:
    venv/bin/python scripts/revoke_clog_echo_completions.py            # dry run
    venv/bin/python scripts/revoke_clog_echo_completions.py --apply
    venv/bin/python scripts/revoke_clog_echo_completions.py --events 9 --apply
"""
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import (  # noqa: E402
    Event, EventBingoCell, EventCompletion, EventTask, EventTeam, session,
)
from services.event_engine import (  # noqa: E402
    CLOG_ECHO_WINDOW_SECONDS, _norm, revoke_ledger_row,
)

APPLIED = ("auto", "confirmed", "manual")


def _target_events(event_ids):
    query = session.query(Event)
    if event_ids:
        query = query.filter(Event.id.in_(event_ids))
    else:
        query = query.filter(Event.status == "active")
    return query.all()


def _echo_rows(event_id):
    """Applied/pending clog rows whose paired drop row credited the same
    (task, team, player, item) inside the echo window."""
    rows = (session.query(EventCompletion)
            .filter(EventCompletion.event_id == event_id,
                    EventCompletion.source_type.in_(("drop", "clog")),
                    EventCompletion.status.in_(APPLIED + ("pending",)))
            .all())
    drops = [r for r in rows if r.source_type == "drop"]
    echoes = []
    for clog in (r for r in rows if r.source_type == "clog"):
        for drop in drops:
            if (drop.task_id == clog.task_id
                    and drop.team_id == clog.team_id
                    and drop.player_id == clog.player_id
                    and _norm(drop.matched_target) == _norm(clog.matched_target)
                    and clog.matched_target
                    and abs((drop.created_at - clog.created_at).total_seconds())
                    <= CLOG_ECHO_WINDOW_SECONDS):
                echoes.append((clog, drop))
                break
    return echoes


def _orphan_task_rows(event, all_orphans=False):
    """Applied non-bonus ledger rows on tasks a bingo board doesn't use.

    Default scope: only tasks that DUPLICATE a cell-bound task (same
    normalized label + type — the board-save cloning artifact, e.g. event 9's
    53/58 "10 Armadyl Points" pair), where every credit is unambiguous double
    counting. ``--all-orphans`` widens it to every task off the board — a
    standings-affecting policy call (e.g. a legit task the admins simply never
    placed), so it is never the default."""
    if not event.has_bingo or (getattr(event, "kind", None) or "") == "board_game":
        return []
    bound = {
        cell.task_id
        for cell in session.query(EventBingoCell)
        .filter(EventBingoCell.event_id == event.id,
                EventBingoCell.task_id.isnot(None)).all()
    }
    tasks = session.query(EventTask).filter(EventTask.event_id == event.id).all()
    bound_keys = {(_norm(t.label), t.type) for t in tasks if t.id in bound}
    orphan_ids = [
        t.id for t in tasks
        if t.id not in bound
        and (all_orphans or (_norm(t.label), t.type) in bound_keys)
    ]
    if not orphan_ids:
        return []
    return (session.query(EventCompletion)
            .filter(EventCompletion.event_id == event.id,
                    EventCompletion.task_id.in_(orphan_ids),
                    EventCompletion.status.in_(APPLIED + ("pending",)))
            .all())


def _revoke(row, note, apply):
    """Mirror the web_api revoke flow: flip the status, then let the engine
    re-fold. Pending rows never applied anything — just flip them."""
    was_applied = row.status in APPLIED
    print(f"  revoke row {row.id} [{row.status}] task {row.task_id} "
          f"team {row.team_id} player {row.player_id} "
          f"{row.source_type} '{row.matched_target}' qty {row.quantity} — {note}")
    if not apply:
        return
    row.status = "revoked"
    row.note = note[:255]
    if was_applied:
        summary = revoke_ledger_row(session, row)
        if summary:
            print(f"    recomputed: progress={summary.get('progress')} "
                  f"completed={summary.get('completed')} "
                  f"team_score={summary.get('team_score')} "
                  f"revoked_bonuses={summary.get('revoked_bonuses') or []}")
    session.flush()


def main(apply, event_ids, all_orphans=False):
    events = _target_events(event_ids)
    if not events:
        print("No matching events.")
        return
    total = 0
    for event in events:
        echoes = _echo_rows(event.id)
        orphans = [r for r in _orphan_task_rows(event, all_orphans=all_orphans)
                   if (r.source_type or "") != "bonus"
                   and r.id not in {c.id for c, _ in echoes}]
        if not echoes and not orphans:
            continue
        scores_before = {
            t.id: (t.name, int(t.score or 0))
            for t in session.query(EventTeam).filter(EventTeam.event_id == event.id)
        }
        print(f"\nEvent {event.id} — {event.name}")
        for clog, drop in echoes:
            _revoke(clog, f"clog echo of drop row {drop.id}", apply)
            total += 1
        for row in orphans:
            _revoke(row, "orphan duplicate task (not on the bingo board)", apply)
            total += 1
        if apply:
            session.flush()
            for team in session.query(EventTeam).filter(EventTeam.event_id == event.id):
                name, before = scores_before.get(team.id, (team.name, 0))
                after = int(team.score or 0)
                if before != after:
                    print(f"  score: {name} {before} -> {after}")
    if apply:
        session.commit()
    print(f"\n{'APPLIED' if apply else 'DRY RUN'}: {total} ledger rows revoked.")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    all_orphans = "--all-orphans" in sys.argv
    ids = []
    for arg in sys.argv[1:]:
        if arg.startswith("--events="):
            ids = [int(x) for x in arg.split("=", 1)[1].split(",") if x.strip()]
        elif arg == "--events":
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv):
                ids = [int(x) for x in sys.argv[idx + 1].split(",") if x.strip()]
    main(apply, ids, all_orphans=all_orphans)
