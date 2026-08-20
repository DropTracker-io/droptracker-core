"""Find (and repair) EHE effort rows inflated by a cross-source KC fold.

Effort counts kills at an event's relevant NPCs. Two sources report an
absolute kill count — a plugin drop's loot-tracker ``kill_count`` and WOM's
boss metric — and until the fix for bug report #131 they shared ONE watermark,
on the assumption that both count the same event. Fortis Colosseum broke that
assumption: WOM's ``sol_heredit`` counts completed runs, the plugin's chest KC
counts every attempt, and the two sat 748 apart for the same player. The fold
credited that LIFETIME DIFFERENCE as kills earned during the event.

``services/event_engine._fold_kc_watermark`` now keeps a baseline per source,
so the leak cannot recur. This script finds rows it already wrote.

Detection is evidence-based rather than rate-based: for a row whose effort came
only from the plugin, the kills the plugin actually witnessed are bounded by
the SPAN of the kill counts it reported inside the event window
(``max - min + 1``), plus any kill-time submissions that credited without one.
A row claiming wildly more than its own evidence supports is the leak — real
grinding moves the counter it is being counted from.

Rows fed by WOM (``source`` = ``wom``/``both``) are reported but never
repaired: WOM legitimately contributes kills no drop ever reported, so the
plugin span is not an upper bound for them.

    ./venv/bin/python -m scripts.audit_event_effort                 # all events
    ./venv/bin/python -m scripts.audit_event_effort --event 46
    ./venv/bin/python -m scripts.audit_event_effort --event 46 --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

sys.path.insert(0, "/store/droptracker/disc")

from sqlalchemy import text  # noqa: E402

#: Flag a row only when it claims this many kills more than its own evidence
#: AND this many times more. Both, so a boss with a handful of unreported kills
#: (a stack that never submitted, a screenshot that failed) is never touched:
#: the leak overshoots by hundreds, not by threes.
ABS_MARGIN = 25
RATIO = 2.0

#: Rows below this are not probed at all. The evidence probe costs three
#: queries against a ~190M-row ``drops`` table, and the leak this hunts
#: overshoots by hundreds — a small row cannot be hiding one. Lower it with
#: --min-kills when auditing a short event.
MIN_KILLS = 50


def _window(event) -> tuple:
    start = event.activated_at or event.starts_at
    end = event.ends_at or datetime.now()
    return start, min(end, datetime.now())


def _plugin_evidence(session, player_id: int, npc_id: int, start, end) -> dict:
    """What this player's own submissions prove about kills at this NPC inside
    the window: the reported kill-count span, how many distinct counts were
    seen, and how many kill-time (PB) submissions landed."""
    row = session.execute(text("""
        SELECT MIN(kill_count) AS lo, MAX(kill_count) AS hi,
               COUNT(DISTINCT kill_count) AS seen
        FROM drops
        WHERE player_id = :pid AND npc_id = :nid
          AND date_added BETWEEN :start AND :end
          AND kill_count > 0
    """), {"pid": player_id, "nid": npc_id, "start": start, "end": end}).first()
    pbs = session.execute(text("""
        SELECT COUNT(*) FROM personal_best
        WHERE player_id = :pid AND npc_id = :nid
          AND date_added BETWEEN :start AND :end
    """), {"pid": player_id, "nid": npc_id, "start": start, "end": end}).scalar() or 0
    lo, hi, seen = (row.lo, row.hi, row.seen) if row else (None, None, 0)
    span = (int(hi) - int(lo) + 1) if lo is not None and hi is not None else 0
    # Drops with no usable kill count credit through the cooldown fallback, so
    # a boss that reports none is bounded by its submission count instead.
    no_kc = session.execute(text("""
        SELECT COUNT(DISTINCT unique_id) FROM drops
        WHERE player_id = :pid AND npc_id = :nid
          AND date_added BETWEEN :start AND :end
          AND (kill_count IS NULL OR kill_count = 0)
    """), {"pid": player_id, "nid": npc_id, "start": start, "end": end}).scalar() or 0
    return {"span": span, "seen": int(seen or 0), "pbs": int(pbs),
            "no_kc": int(no_kc), "lo": lo, "hi": hi}


def audit(session, event_ids, apply: bool, min_kills: int = MIN_KILLS) -> int:
    from db.models import Event, EventEffort

    flagged = 0
    events = session.query(Event).filter(Event.id.in_(event_ids)).all() \
        if event_ids else session.query(Event).all()
    for event in sorted(events, key=lambda e: e.id):
        rows = (session.query(EventEffort)
                .filter(EventEffort.event_id == event.id,
                        EventEffort.kills >= min_kills)
                .order_by(EventEffort.kills.desc()).all())
        if not rows:
            continue
        start, end = _window(event)
        if start is None:
            print(f"event {event.id} ({event.name}): no start — skipped")
            continue
        header_shown = False
        for row in rows:
            ev = _plugin_evidence(session, row.player_id, row.npc_id, start, end)
            # The most generous reading of the player's own submissions.
            supported = max(ev["span"], ev["seen"], ev["pbs"] + ev["no_kc"])
            kills = int(row.kills or 0)
            if kills <= supported + ABS_MARGIN or kills < supported * RATIO:
                continue
            flagged += 1
            if not header_shown:
                print(f"\nevent {event.id} — {event.name} "
                      f"[{start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M}]")
                header_shown = True
            npc = session.execute(
                text("SELECT npc_name FROM npc_list WHERE npc_id = :n"),
                {"n": row.npc_id}).scalar() or row.npc_id
            print(f"  player {row.player_id:>8} {str(npc)[:28]:<28} "
                  f"source={row.source or '?':<6} kills={kills:<6} "
                  f"evidence={supported:<5} "
                  f"(kc {ev['lo']}..{ev['hi']}, {ev['seen']} seen, "
                  f"{ev['pbs']} pb, {ev['no_kc']} no-kc)")
            if row.source not in (None, "plugin"):
                print("      ^ WOM-fed: reported only, not repaired "
                      "(WOM sees kills no drop reports)")
                continue
            print(f"      -> {kills} => {supported}")
            if apply:
                row.kills = supported
    return flagged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=int, action="append",
                        help="limit to this event id (repeatable)")
    parser.add_argument("--apply", action="store_true",
                        help="write the corrected kill counts (default: dry run)")
    parser.add_argument("--min-kills", type=int, default=MIN_KILLS,
                        help=f"skip rows below this many kills (default {MIN_KILLS})")
    args = parser.parse_args()

    from db import Session

    with Session() as session:
        flagged = audit(session, args.event or [], args.apply, args.min_kills)
        if args.apply and flagged:
            session.commit()
            print(f"\ncommitted corrections for {flagged} row(s)")
        else:
            session.rollback()
            print(f"\n{flagged} row(s) flagged"
                  f"{'' if flagged == 0 else ' — re-run with --apply to correct'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
