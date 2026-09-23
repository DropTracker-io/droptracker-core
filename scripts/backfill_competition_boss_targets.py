"""Tag a boss race's existing kill rows with the boss they came from.

Since the per-boss breakdown shipped, the engine writes the boss name into
``web_event_completions.matched_target`` on every gained-kill row of a BOTW
race (``services.event_engine.match_task``), and ``services.competition``
folds kills per boss from it. Rows written before that carry NULL and fold
under "unattributed". Everything needed to name their boss is on record:

* ``source_type='drop'`` rows keep the drop id in ``source_id``, and the drop
  row has its ``npc_id``.
* ``source_type='wom_kc'`` rows carry the WOM boss metric in their guid
  (``wom:{event}:{wom_player}:kc:{metric}:{ts}``), and the task config's
  NPC list maps it back, exactly as the live matcher does.

Only untagged gained rows (``note IS NULL``, ``matched_target IS NULL``) on
boss races are touched, and only with a name the race actually tracks, so the
script is idempotent and safe to re-run. A frozen (ended) race keeps its
frozen standings; re-tagging it changes nothing a viewer sees.

Usage
-----
    python -m scripts.backfill_competition_boss_targets              # dry run
    python -m scripts.backfill_competition_boss_targets --event 86
    python -m scripts.backfill_competition_boss_targets --apply
"""

import argparse
import sys
from collections import Counter

sys.path.insert(0, ".")

_CHUNK = 500


def _wom_metric(guid):
    parts = str(guid or "").split(":")
    # wom:{event}:{wom_player}:kc:{metric}:{ts}
    if len(parts) >= 6 and parts[0] == "wom" and parts[3] == "kc":
        return parts[4]
    return None


def _backfill_task(s, task, apply: bool) -> Counter:
    from sqlalchemy import bindparam, text

    from services.competition import CompetitionConfig, _norm
    from services.event_engine import _task_to_dict, _kc_wom_metrics

    config = CompetitionConfig(task.config)
    stats: Counter = Counter()
    if config.metric_kind != "boss" or not config.npcs:
        return stats
    raced = set(config.npcs)
    wom_map = _kc_wom_metrics(_task_to_dict(task))

    rows = s.execute(text("""
        SELECT id, source_type, source_id, submission_guid
          FROM web_event_completions
         WHERE task_id = :tid AND note IS NULL AND matched_target IS NULL
    """), {"tid": task.id}).fetchall()

    updates: dict = {}
    drop_rows: dict = {}
    for rid, source_type, source_id, guid in rows:
        if source_type == "drop" and source_id is not None:
            drop_rows.setdefault(int(source_id), []).append(rid)
        elif source_type == "wom_kc":
            npc = wom_map.get(_wom_metric(guid) or "")
            if npc:
                updates[rid] = npc.title()
            else:
                stats["wom_unmapped"] += 1
        else:
            stats[f"skipped_{source_type or 'none'}"] += 1

    drop_ids = sorted(drop_rows)
    for i in range(0, len(drop_ids), _CHUNK):
        chunk = drop_ids[i:i + _CHUNK]
        found = s.execute(text("""
            SELECT d.drop_id, n.npc_name
              FROM drops d JOIN npc_list n ON n.npc_id = d.npc_id
             WHERE d.drop_id IN :ids
        """).bindparams(bindparam("ids", expanding=True)),
            {"ids": chunk}).fetchall()
        names = {int(did): name for did, name in found}
        for did in chunk:
            name = names.get(did)
            if name and _norm(name) in raced:
                for rid in drop_rows[did]:
                    updates[rid] = str(name).strip()[:120]
            else:
                stats["drop_unmapped"] += len(drop_rows[did])

    for name in updates.values():
        stats[f"tag:{_norm(name)}"] += 1
    stats["tagged"] = len(updates)

    if apply and updates:
        by_name: dict = {}
        for rid, name in updates.items():
            by_name.setdefault(name, []).append(rid)
        for name, ids in by_name.items():
            for i in range(0, len(ids), _CHUNK):
                s.execute(text("""
                    UPDATE web_event_completions SET matched_target = :name
                     WHERE id IN :ids AND matched_target IS NULL AND note IS NULL
                """).bindparams(bindparam("ids", expanding=True)),
                    {"name": name, "ids": ids[i:i + _CHUNK]})
        s.commit()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--event", type=int, help="only this event id")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args()

    from db.models import Event, EventTask
    from db.models.base import Session

    s = Session()
    try:
        q = (s.query(EventTask, Event)
             .join(Event, Event.id == EventTask.event_id)
             .filter(EventTask.type == "competition", Event.kind == "botw"))
        if args.event:
            q = q.filter(Event.id == args.event)
        for task, event in q.order_by(Event.id).all():
            stats = _backfill_task(s, task, args.apply)
            if not stats:
                continue
            print(f"event {event.id} ({event.status}) task {task.id}: "
                  + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
        if not args.apply:
            print("dry run: nothing written (pass --apply)")
            s.rollback()
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
