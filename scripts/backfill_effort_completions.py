"""Backfill ``web_event_effort.completions`` from the drops that prove them.

``services/event_effort.COMPLETION_MARKERS`` NPCs keep two counters, because
the content pays out for a partial attempt and so the plugin's KC and WOM's
boss metric count different events. The counter only started being written
when web104a shipped; every row recorded before that has ``completions = 0``,
which the read model reads as "all attempts were bails" and prices at the
partial rate — the opposite error to the one the split exists to fix.

Evidence is the same thing the live path uses: an attempt is one
(player, kill_count) pair at the marker NPC inside the event window, and it
completed if any of its drops was the marker item. Rows for players whose
plugin never reported (WOM-fed) legitimately have no drops to count, so a
backfilled 0 there is not evidence of anything — those are reported and left
alone, exactly as ``audit_event_effort`` treats WOM-fed rows.

    python -m scripts.backfill_effort_completions            # dry run
    python -m scripts.backfill_effort_completions --apply
    python -m scripts.backfill_effort_completions --event 46
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

sys.path.insert(0, "/store/droptracker/disc")

from sqlalchemy import text  # noqa: E402


def _window(event) -> tuple:
    start = event.activated_at or event.starts_at
    end = event.ends_at or datetime.now()
    return start, min(end, datetime.now())


def _completions(session, player_id: int, npc_id: int, marker_item: str,
                 start, end) -> int:
    """Attempts inside the window whose loot included the marker item."""
    return int(session.execute(text("""
        SELECT COUNT(*) FROM (
            SELECT d.kill_count
            FROM drops d JOIN items i ON i.item_id = d.item_id
            WHERE d.player_id = :p AND d.npc_id = :n
              AND d.date_added >= :s AND d.date_added <= :e
              AND d.kill_count IS NOT NULL
            GROUP BY d.kill_count
            HAVING MAX(i.item_name = :item) = 1
        ) c
    """), {"p": player_id, "n": npc_id, "item": marker_item,
           "s": start, "e": end}).scalar() or 0)


def backfill(session, event_ids, apply: bool) -> tuple:
    from db.models import Event, EventEffort
    from services.event_effort import COMPLETION_MARKERS

    # Marker NPCs by id, resolved once.
    markers = {}
    for npc_norm, marker in COMPLETION_MARKERS.items():
        row = session.execute(text(
            "SELECT npc_id, npc_name FROM npc_list WHERE LOWER(npc_name) = :n"),
            {"n": npc_norm}).fetchone()
        if row is None:
            print(f"no npc_list row for marker NPC {npc_norm!r} — skipped")
            continue
        markers[int(row[0])] = (row[1], marker["item"])
    if not markers:
        return 0, 0

    events = session.query(Event).filter(Event.id.in_(event_ids)).all() \
        if event_ids else session.query(Event).all()
    seen = changed = 0
    for event in sorted(events, key=lambda e: e.id):
        rows = (session.query(EventEffort)
                .filter(EventEffort.event_id == event.id,
                        EventEffort.npc_id.in_(sorted(markers)))
                .order_by(EventEffort.kills.desc()).all())
        if not rows:
            continue
        start, end = _window(event)
        if start is None:
            print(f"event {event.id} ({event.name}): no start — skipped")
            continue
        print(f"\nevent {event.id} — {event.name} "
              f"[{start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M}]")
        for row in rows:
            seen += 1
            name, item = markers[int(row.npc_id)]
            found = _completions(session, row.player_id, int(row.npc_id),
                                 item, start, end)
            kills = int(row.kills or 0)
            was = int(row.completions or 0)
            note = ""
            if found == 0 and row.source in ("wom", "both"):
                # No plugin drops to read; WOM's own fold will fill this in on
                # its next pass. Writing 0 would assert "all bails".
                note = "  WOM-fed with no marker drops — left alone"
                print(f"  player {row.player_id:>8} {str(name)[:24]:<24} "
                      f"kills={kills:<5} completions={was}{note}")
                continue
            if found == was:
                continue
            print(f"  player {row.player_id:>8} {str(name)[:24]:<24} "
                  f"kills={kills:<5} completions {was} => {found}"
                  f"  ({kills - found} partial)")
            changed += 1
            if apply:
                row.completions = found
    return seen, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=int, action="append",
                        help="limit to this event id (repeatable)")
    parser.add_argument("--apply", action="store_true",
                        help="write the counts (default: dry run)")
    args = parser.parse_args()

    from db import Session

    with Session() as session:
        seen, changed = backfill(session, args.event or [], args.apply)
        if args.apply and changed:
            session.commit()
            print(f"\ncommitted {changed} of {seen} marker row(s)")
        else:
            session.rollback()
            print(f"\n{changed} of {seen} marker row(s) would change"
                  f"{'' if changed == 0 else ' — re-run with --apply'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
