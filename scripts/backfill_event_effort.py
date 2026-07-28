"""Seed Bingo EHB effort rows for an event that was already running (web72a).

Effort is normally accumulated live, so an event that started before the
feature shipped shows every participant at zero. WOM's ``bulk-gained`` answers
the question retroactively — it returns per-member start/end values for every
metric over an arbitrary window — so one call per participating clan
reconstructs the whole event's boss kills.

What it CANNOT reconstruct is freeze timing: we only know a task is complete
*now*, not the exact kill it completed on. Rows for NPCs whose tasks are
already done are stamped ``frozen_at`` from ``EventProgress.completed_at``, so
the report reads correctly, but kills earned after that completion are still
counted. Accept it or don't backfill — the alternative is pretending to a
precision the data doesn't have.

Only bosses on the WOM hiscores are recoverable. Plugin-only sources (chest
encounters, activity containers) start from zero and accumulate from now.

Usage
-----
    python -m scripts.backfill_event_effort --event 42            # dry run
    python -m scripts.backfill_event_effort --event 42 --apply
    python -m scripts.backfill_event_effort --all-active --apply
"""

import argparse
import asyncio
import sys
from datetime import datetime

sys.path.insert(0, ".")


def _norm(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _event_window(ev):
    """The [start, end] the backfill asks WOM about — the same effective window
    the matcher uses (scheduled dates narrowed by explicit activate/end)."""
    starts = [d for d in (ev.starts_at, ev.activated_at) if d is not None]
    ends = [d for d in (ev.ends_at, ev.ended_at) if d is not None]
    start = max(starts) if starts else None
    end = min(ends) if ends else None
    now = datetime.now()
    if end is None or end > now:
        end = now
    return start, end


def _participating_group_ids(session, ev) -> list:
    from db.models import EventGroup

    gids = {ev.group_id} if ev.group_id else set()
    for eg in (session.query(EventGroup)
               .filter(EventGroup.event_id == ev.id,
                       EventGroup.status == "accepted").all()):
        gids.add(eg.group_id)
    return sorted(g for g in gids if g)


def _roster(session, event_id):
    """``({wom_id: entry}, {normalized name: entry})`` where entry is
    ``(player_id, team_id, joined_at)`` — the two ways a WOM row identifies a
    participant, mirroring the live reconciler's matching."""
    from db.models import EventTeamMember, Player

    by_wom, by_name = {}, {}
    rows = (session.query(EventTeamMember.player_id, EventTeamMember.team_id,
                          EventTeamMember.joined_at, Player.wom_id, Player.player_name)
            .outerjoin(Player, Player.player_id == EventTeamMember.player_id)
            .filter(EventTeamMember.event_id == event_id)
            .all())
    for player_id, team_id, joined_at, wom_id, player_name in rows:
        entry = (int(player_id), team_id, joined_at)
        if wom_id:
            by_wom[int(wom_id)] = entry
        if player_name:
            by_name[_norm(player_name)] = entry
    return by_wom, by_name


def _frozen_npc_ids(session, event_id, npcs) -> dict:
    """``{team_id: {npc_id: completed_at}}`` for NPCs whose every task is done.
    ``completed_at`` is the LATEST of the NPC's tasks — the moment it truly
    stopped being relevant for that team."""
    from db.models import EventProgress

    done: dict = {}
    for team_id, task_id, completed_at in (
            session.query(EventProgress.team_id, EventProgress.task_id,
                          EventProgress.completed_at)
            .filter(EventProgress.event_id == event_id,
                    EventProgress.completed.is_(True)).all()):
        if team_id is None:
            continue
        done.setdefault(team_id, {})[int(task_id)] = completed_at

    out: dict = {}
    for team_id, task_map in done.items():
        for entry in npcs.values():
            npc_id, tasks = entry.get("npc_id"), entry.get("tasks") or []
            if not npc_id or not tasks:
                continue
            if not all(int(t) in task_map for t in tasks):
                continue
            stamps = [task_map[int(t)] for t in tasks if task_map.get(int(t))]
            out.setdefault(team_id, {})[int(npc_id)] = max(stamps) if stamps else None
    return out


async def _bulk_rows(session, ev):
    """Every bulk-gained row across the event's participating clans."""
    from db.models import Group
    from utils.wiseoldman import get_group_bulk_gained

    start, end = _event_window(ev)
    if start is None:
        print(f"  event {ev.id}: no start date — nothing to ask WOM about")
        return []
    gids = _participating_group_ids(session, ev)
    wom_ids = [
        int(w) for (w,) in session.query(Group.wom_id)
        .filter(Group.group_id.in_(gids), Group.wom_id.isnot(None)).all() if w
    ]
    if not wom_ids:
        print(f"  event {ev.id}: no clan has a WOM id — nothing to backfill")
        return []
    rows = []
    for wom_gid in wom_ids:
        fetched = await get_group_bulk_gained(wom_gid, start, end)
        print(f"  wom group {wom_gid}: {len(fetched or [])} member row(s)")
        rows.extend(fetched or [])
    return rows


def _backfill_event(session, redis_conn, ev, rows, *, apply: bool) -> int:
    from db.models import EventEffort
    from services.event_effort import effort_scope
    from services.event_engine import _STATE_KEY_TTL, _task_to_dict, load_effort_npcs
    from db.models import EventTask

    tasks = [_task_to_dict(t) for t in
             session.query(EventTask).filter(EventTask.event_id == ev.id).all()]
    npcs = load_effort_npcs(session, redis_conn, ev.id, tasks)
    if not npcs:
        print(f"  event {ev.id}: no relevant NPCs resolved")
        return 0
    by_metric = {e["metric"]: (name, e) for name, e in npcs.items() if e.get("metric")}
    print(f"  {len(npcs)} relevant NPC(s), {len(by_metric)} with a WOM metric")

    by_wom, by_name = _roster(session, ev.id)
    frozen = _frozen_npc_ids(session, ev.id, npcs)
    _start, window_end = _event_window(ev)
    written = 0

    for row in rows:
        player_obj = (row or {}).get("player") or {}
        entry = None
        wom_id = player_obj.get("id")
        if wom_id:
            entry = by_wom.get(int(wom_id))
        if entry is None:
            entry = by_name.get(_norm(player_obj.get("displayName")
                                      or player_obj.get("username")))
        if entry is None:
            continue
        player_id, team_id, _joined_at = entry
        metrics = {m.get("metric"): m for m in (row.get("data") or [])
                   if isinstance(m, dict)}
        for slug, (npc_name, npc_entry) in by_metric.items():
            m = metrics.get(slug)
            if not m:
                continue
            try:
                end_kc = int(m.get("end") or 0)
                start_kc = int(m.get("start") or 0)
            except (TypeError, ValueError):
                continue
            gained = max(end_kc - max(start_kc, 0), 0)
            if gained <= 0:
                continue
            npc_id = npc_entry.get("npc_id")
            if not npc_id:
                continue
            frozen_at = frozen.get(team_id, {}).get(int(npc_id))
            existing = (session.query(EventEffort)
                        .filter(EventEffort.event_id == ev.id,
                                EventEffort.player_id == player_id,
                                EventEffort.npc_id == npc_id).first())
            # Never lower a live-accumulated count: the backfill is a floor
            # ("at least this many"), not a correction of what we watched.
            if existing is not None and int(existing.kills or 0) >= gained:
                continue
            print(f"    player {player_id} {npc_name}: "
                  f"{int(existing.kills) if existing else 0} -> {gained}"
                  f"{' (frozen)' if frozen_at else ''}")
            written += 1
            if not apply:
                continue
            if existing is None:
                session.add(EventEffort(
                    event_id=ev.id, team_id=team_id, player_id=player_id,
                    npc_id=npc_id, boss_metric=slug, kills=gained, source="wom",
                    first_at=window_end, last_at=window_end, frozen_at=frozen_at))
            else:
                existing.kills = gained
                existing.source = "both" if existing.source == "plugin" else "wom"
                if frozen_at and existing.frozen_at is None:
                    existing.frozen_at = frozen_at
            # Seed the watermark to the absolute value we just banked, so the
            # next live fold measures from here instead of re-crediting the
            # whole window (or, on the plugin path, an extra +1 first kill).
            if redis_conn is not None:
                try:
                    redis_conn.set(
                        f"events:{ev.id}:kcbase:{effort_scope(npc_name)}:{player_id}",
                        end_kc, ex=_STATE_KEY_TTL)
                except Exception:
                    pass
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--event", type=int, help="event id to backfill")
    group.add_argument("--all-active", action="store_true",
                       help="every currently active event")
    parser.add_argument("--apply", action="store_true",
                        help="write the rows (default: dry run)")
    args = parser.parse_args()

    from db import Session
    from db.models import Event
    from utils.redis import redis_client

    session = Session()
    redis_conn = getattr(redis_client, "client", None)
    try:
        q = session.query(Event)
        events = (q.filter(Event.status == "active").all() if args.all_active
                  else q.filter(Event.id == args.event).all())
        if not events:
            sys.exit("no matching event(s)")
        # One event loop for the whole run: the shared wom.Client holds an
        # aiohttp session bound to whichever loop started it, so a second
        # asyncio.run() would fetch into a closed loop and silently return no
        # rows for every event after the first.
        async def _fetch_all():
            out = []
            for ev in events:
                print(f"event {ev.id}: {ev.name!r} ({ev.status})")
                out.append((ev, await _bulk_rows(session, ev)))
            return out

        total = 0
        for ev, rows in asyncio.run(_fetch_all()):
            if rows:
                total += _backfill_event(session, redis_conn, ev, rows, apply=args.apply)
        if args.apply:
            session.commit()
            print(f"\napplied: {total} effort row(s) written")
        else:
            print(f"\ndry run: {total} effort row(s) would be written "
                  f"(re-run with --apply)")
    finally:
        session.close()


if __name__ == "__main__":
    main()
