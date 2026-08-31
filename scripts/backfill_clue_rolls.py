"""Fill ``web_event_effort.rolls`` for events that were already running.

Clue tiers were priced at 0 EHE before suggestion #156, so the openings are on
record but the scrolls that dated them are not. Everything needed to reconstruct
the roll counter is already in ``drops``: a roll is a receipt item
(``services.event_effort.CLUE_SOURCE_PREFIXES``) landing inside the event's
effective window, and the effort row it belongs to is the tier's pseudo-NPC.

The count is **recomputed and SET**, never incremented, so the script is
idempotent and safe to re-run: ``drops`` is the same source the live path reads,
so a second run simply realigns the column to it.

What it deliberately does NOT do:

* Create effort rows for a player who rolled clues but opened no caskets in the
  window. ``min(rolled, opened)`` is 0 for them either way, and inventing rows
  would put a 0-hour NPC on their card that they never touched.
* Touch events with a recurring ``schedule_config``. Effort there accrues only
  inside the materialized sub-windows, which this script does not resolve —
  those events are listed and skipped rather than counted wrong.
* Count rolls past a row's ``frozen_at``. The live path stops crediting a tier
  the moment every task it feeds is complete for the team, so a window-wide
  count would credit scrolls the running system would have refused. The
  over-credit is bounded by ``kills`` (frozen at the same instant), which is
  why it stayed invisible on event 46 — all 53 of its rolls predate the freeze
  — but a player who kept doing clues after their tile finished would have been
  paid for openings the live path had already stopped counting.

Usage
-----
    python -m scripts.backfill_clue_rolls              # dry run, all events
    python -m scripts.backfill_clue_rolls --event 46
    python -m scripts.backfill_clue_rolls --apply
"""

import argparse
import sys

sys.path.insert(0, ".")


#: Receipts of one tier for one player inside one window. ``quantity`` is summed
#: rather than rows counted: a stacked receipt is still that many clues.
_ROLLS_SQL = """
    SELECT COALESCE(SUM(d.quantity), 0)
      FROM drops d
      JOIN items i ON i.item_id = d.item_id
     WHERE d.player_id = :pid
       AND i.item_name IN :names
       AND d.date_added >= :start
       AND (:end IS NULL OR d.date_added <= :end)
"""


def _window(event, row):
    """The window a single effort row may count rolls in.

    The event's effective window (exactly as ``_event_to_dict`` computes it),
    closed early at the row's ``frozen_at`` so the count matches what the live
    path would have credited.
    """
    starts = [d for d in (event.starts_at, event.activated_at) if d is not None]
    ends = [d for d in (event.ends_at, event.ended_at, row.frozen_at)
            if d is not None]
    return (max(starts) if starts else None), (min(ends) if ends else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", type=int, action="append",
                    help="only this event id (repeatable)")
    ap.add_argument("--apply", action="store_true", help="write the rolls")
    args = ap.parse_args()

    from sqlalchemy import bindparam, text

    from db import session
    from db.models import Event, EventEffort, NpcList
    from services.event_effort import CLUE_SOURCE_PREFIXES, CLUE_TIERS

    # {npc_id: [receipt item names]} for every clue pseudo-NPC in npc_list.
    tiers_by_name = {name.lower(): entry for name, entry in CLUE_TIERS.items()}
    receipts_by_npc_id: dict = {}
    npc_names: dict = {}
    for npc_id, npc_name in session.query(NpcList.npc_id, NpcList.npc_name):
        entry = tiers_by_name.get(" ".join((npc_name or "").lower().split()))
        if entry is None:
            continue
        receipts_by_npc_id[int(npc_id)] = [
            f"{prefix} ({entry['tier']})" for prefix in CLUE_SOURCE_PREFIXES
        ]
        npc_names[int(npc_id)] = npc_name

    ROLLS_SQL = text(_ROLLS_SQL).bindparams(bindparam("names", expanding=True))

    rows_q = (session.query(EventEffort)
              .filter(EventEffort.npc_id.in_(sorted(receipts_by_npc_id))))
    if args.event:
        rows_q = rows_q.filter(EventEffort.event_id.in_(args.event))
    rows = rows_q.all()
    if not rows:
        print("no clue effort rows to backfill")
        return 0

    events = {e.id: e for e in session.query(Event).filter(
        Event.id.in_(sorted({r.event_id for r in rows})))}

    changed = skipped_scheduled = skipped_window = 0
    per_event: dict = {}
    for row in rows:
        event = events.get(row.event_id)
        if event is None:
            continue
        if getattr(event, "schedule_config", None):
            skipped_scheduled += 1
            continue
        start, end = _window(event, row)
        if start is None:
            skipped_window += 1
            continue
        names = receipts_by_npc_id[int(row.npc_id)]
        rolled = session.execute(
            ROLLS_SQL,
            {"pid": row.player_id, "names": names, "start": start, "end": end},
        ).scalar() or 0
        rolled = int(rolled)
        if rolled == int(row.rolls or 0):
            continue
        bucket = per_event.setdefault(row.event_id, [])
        bucket.append((row.player_id, npc_names.get(int(row.npc_id)),
                       int(row.kills or 0), int(row.rolls or 0), rolled))
        row.rolls = rolled
        changed += 1

    for event_id, entries in sorted(per_event.items()):
        event = events[event_id]
        print(f"\nevent {event_id} — {event.name}")
        for player_id, npc, kills, was, now in sorted(entries):
            paired = min(kills, now)
            print(f"  player {player_id:<8} {npc:<22} "
                  f"opened {kills:<5} rolls {was} -> {now:<5} "
                  f"(pays for {paired})")

    print(f"\n{changed} row(s) to update"
          f"{'' if args.apply else ' (dry run)'}"
          f"; {skipped_scheduled} skipped on recurring schedules"
          f", {skipped_window} with no window start")
    if args.apply and changed:
        session.commit()
        print("committed")
    elif changed:
        session.rollback()
        print("re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
