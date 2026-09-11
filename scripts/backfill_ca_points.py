"""Fill in ``player_ca_varps.points`` for the players whose total we can know.

Nothing wrote the column before web111a, so the Data API reported
``points: null`` for everyone. The live writers keep it current from here on;
this seeds it from what is already stored, in two passes that both go through
``db.ca_points.record_ca_points`` — exactly the rule the live writers obey:

1. **sync** — every synced row: what its stored completion bits are worth,
   counted against the task registry. Dated when the bits last changed.
2. **history** — the game's own total, which every completion carries, from
   the completion notifications still in ``notification_queue`` (~30 days).
   Used only for a player whose most recent recorded completion is the very
   one that notification carried, so the total is current as of their last
   task. This is what covers players who complete tasks but have never synced
   (468 of them on 2026-09-10). Applied as *non-authoritative*: a
   notification's timestamp is when we processed it, not when the game was
   read, so it may raise a total but never lower one.

Both passes run per batch of players in one short transaction each, so no
row lock is held long enough for the webhook consumer to wait on it. The dry
run executes the real rule and rolls every batch back, so its numbers are
exact rather than estimated.

Idempotent — re-running changes nothing — and dry-run by default:

    ./venv/bin/python -m scripts.backfill_ca_points
    ./venv/bin/python -m scripts.backfill_ca_points --apply

To re-count every sync-derived total after a registry correction, clear them
first (``UPDATE player_ca_varps SET points = NULL WHERE points_source =
'sync'``) and re-run: a count may only ever raise a stored total.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime

from sqlalchemy import text

BATCH = 200
NOTIFICATION_TYPES = ("ca", "dm_ca")


def _chunks(items, size):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _synced_rows(session):
    """player_id -> (varps, updated_at) for every row that holds bits.

    A row whose date is not a real moment (MariaDB hands back the zero-date as
    a string) cannot be ordered against anything, so it is left out.
    """
    rows = session.execute(text(
        "SELECT player_id, varps, updated_at FROM player_ca_varps WHERE varps IS NOT NULL"
    ))
    return {int(pid): (varps, updated_at) for pid, varps, updated_at in rows
            if isinstance(updated_at, datetime)}


def _history_readings(session):
    """player_id -> (total, recorded_at): the game's total at the player's most
    recent recorded completion, when a notification carried it."""
    by_guid = {}
    rows = session.execute(text(
        "SELECT player_id, data FROM notification_queue "
        "WHERE notification_type IN :types"
    ).bindparams(types=NOTIFICATION_TYPES))
    for player_id, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        # League totals belong to a different account.
        if (data.get("world_type") or "main") != "main":
            continue
        guid = data.get("guid")
        try:
            total = int(data.get("points_total") or 0)
        except (TypeError, ValueError):
            continue
        if guid and total > 0:
            by_guid[str(guid)] = (int(player_id), total)

    readings = {}
    players = sorted({pid for pid, _ in by_guid.values()})
    for chunk in _chunks(players, 1000):
        # The newest completion row per player; a total is only current if it
        # came with that one.
        latest = session.execute(text("""
            SELECT c.player_id, c.unique_id, c.date_added
            FROM combat_achievement c
            JOIN (SELECT player_id, MAX(id) AS id FROM combat_achievement
                  WHERE player_id IN :ids GROUP BY player_id) m ON m.id = c.id
        """).bindparams(ids=tuple(chunk)))
        for player_id, guid, date_added in latest:
            hit = by_guid.get(str(guid)) if guid else None
            if hit and hit[0] == int(player_id) and isinstance(date_added, datetime):
                readings[int(player_id)] = (hit[1], date_added)
    return readings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    from db.ca_points import (
        SOURCE_GAME, SOURCE_SYNC, load_task_registry, record_ca_points,
    )
    from db.models import Session
    from services.ca_tiers import FALLBACK_TIER_POINTS, ca_tier_summary
    from services.state_sync import combat_achievement_points, deserialize_varps

    with Session() as session:
        tasks = load_task_registry(session)
        if not tasks:
            print("ABORT: the combat achievement task registry is empty — "
                  "run scripts/build_manifest.py first.")
            return 1
        synced = _synced_rows(session)
        history = _history_readings(session)
        session.rollback()

    print(f"registry: {len(tasks)} tasks")
    print(f"synced rows with bits: {len(synced)}")
    print(f"players with a current in-game total in notification history: {len(history)} "
          f"({len(set(history) - set(synced))} have never synced)")

    changed = Counter()
    agreement = Counter()
    tiers = Counter()
    sources = Counter()
    players = sorted(set(synced) | set(history))

    for chunk in _chunks(players, BATCH):
        with Session() as session:
            for player_id in chunk:
                counted = None
                if player_id in synced:
                    varps, bits_changed_at = synced[player_id]
                    counted = combat_achievement_points(deserialize_varps(varps), tasks)
                    if counted is not None and record_ca_points(
                        session, player_id, counted, SOURCE_SYNC, bits_changed_at,
                        # Unchanged bits keep their date: filling in the
                        # total is not a change to the player's progress.
                        now=bits_changed_at,
                    ):
                        changed[SOURCE_SYNC] += 1
                if player_id in history:
                    total, recorded_at = history[player_id]
                    when = max(recorded_at, synced[player_id][1]) if player_id in synced else recorded_at
                    if record_ca_points(
                        session, player_id, total, SOURCE_GAME, recorded_at,
                        authoritative=False, now=when,
                    ):
                        changed[SOURCE_GAME] += 1
                    if counted is not None:
                        agreement["equal" if counted == total else
                                  "game higher" if total > counted else "count higher"] += 1

            for player_id, points, source in session.execute(text(
                "SELECT player_id, points, points_source FROM player_ca_varps "
                "WHERE player_id IN :ids"
            ).bindparams(ids=tuple(chunk))):
                tiers[ca_tier_summary(points, FALLBACK_TIER_POINTS)["tier"] or
                      ("no tier yet" if points is not None else "no total")] += 1
                sources[source or "none"] += 1

            if args.apply:
                session.commit()
            else:
                session.rollback()

    verb = "wrote" if args.apply else "would write"
    print(f"\n{verb}: {changed[SOURCE_SYNC]} totals from synced bits, "
          f"{changed[SOURCE_GAME]} from in-game totals")
    print(f"where both exist: {dict(agreement)}")
    print(f"resulting totals by source: {dict(sources)}")
    order = ["no total", "no tier yet", "Easy", "Medium", "Hard", "Elite", "Master", "Grandmaster"]
    print("resulting tiers: " + ", ".join(f"{t} {tiers[t]}" for t in order if tiers[t]))
    if not args.apply:
        print("\n(dry run — every batch was rolled back; pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
