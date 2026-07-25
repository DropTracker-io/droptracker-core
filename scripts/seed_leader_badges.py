"""Seed the held global loot-leader badges (all-time + monthly).

These two rows are what turns ``services/badges.evaluate_global_champion`` on:
the evaluator is selected by the badge row's ``criteria`` JSON
(``{"type": "global_champion", "period": "all" | "month"}``), so a definition
without criteria stays manual-only and is never awarded automatically.

  global_loot_leader_alltime  - top of ``leaderboard:all`` (one slot, "all")
  global_loot_leader_monthly  - top of ``leaderboard:{YYYYMM}`` (one slot per
                                month; a finished month keeps its winner)

The row's ``semantic`` decides how it is awarded and stays the admin's call:
``held`` = a live crown that changes hands as the board does, ``permanent`` =
a trophy handed out once the month is over. An all-time badge must be ``held``
(that board never closes), which this script warns about but does not force.

Idempotent. Existing rows keep their admin-editable fields (name, description,
icon, tone, semantic) — only missing rows are created, and ``criteria`` is only
written when it differs. ``--force`` also rewrites name/description/tone/
semantic from the defaults below, clobbering admin edits.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.seed_leader_badges
     (dry run; add --apply to write)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Badge, Session  # noqa: E402

# Mirrors the rows as configured on the live site; only used verbatim when a
# row is missing (or with --force).
DEFINITIONS = [
    {
        "key": "global_loot_leader_alltime",
        "name": "Highest Tracked Loot (All Time)",
        "description": ("Awarded to the tracked player who has received the most loot "
                        "through the entire history of the DropTracker."),
        "tone": "bronze",
        "icon_emoji": "🥇",
        "semantic": "held",  # the all-time board never closes; must be held
        "scope": "global",
        "criteria": {"type": "global_champion", "period": "all"},
    },
    {
        "key": "global_loot_leader_monthly",
        "name": "Highest Tracked Loot (Month)",
        "description": ("Awarded to players for achieving the highest amount of loot "
                        "earned during an entire calendar month."),
        "tone": "sky",
        "icon_emoji": "🏅",
        # A month-end trophy. Switch to 'held' for a live crown that changes
        # hands mid-month (evaluate_global_champion supports both).
        "semantic": "permanent",
        "scope": "global",
        "criteria": {"type": "global_champion", "period": "month"},
    },
]


def seed(session=None, apply: bool = False, force: bool = False) -> dict:
    session = session or Session()
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    try:
        for spec in DEFINITIONS:
            criteria = json.dumps(spec["criteria"])
            row = session.query(Badge).filter(Badge.key == spec["key"]).first()
            if row is None:
                print(f"  CREATE {spec['key']}: {spec['name']} "
                      f"({spec['semantic']}, criteria={criteria})")
                counts["created"] += 1
                if apply:
                    session.add(Badge(
                        key=spec["key"],
                        name=spec["name"],
                        description=spec["description"],
                        tone=spec["tone"],
                        icon_emoji=spec["icon_emoji"],
                        semantic=spec["semantic"],
                        scope=spec["scope"],
                        active=True,
                        criteria=criteria,
                    ))
                continue

            if spec["criteria"]["period"] == "all" and row.semantic != "held":
                # The all-time board never closes, so the 'permanent' path
                # (award only a finished period) would never fire.
                print(f"  WARN   {spec['key']}: semantic is {row.semantic!r} — an "
                      f"all-time leader badge must be 'held' to ever be awarded")

            changes = []
            if row.criteria != criteria:
                changes.append(f"criteria {row.criteria!r} -> {criteria!r}")
            if not row.active:
                changes.append("active False -> True")
            if force:
                for field in ("name", "description", "tone", "scope", "semantic"):
                    if getattr(row, field) != spec[field]:
                        changes.append(f"{field} {getattr(row, field)!r} -> {spec[field]!r}")
            if not changes:
                print(f"  OK     {spec['key']}: already wired (badge_id={row.badge_id})")
                counts["unchanged"] += 1
                continue

            print(f"  UPDATE {spec['key']} (badge_id={row.badge_id}): " + "; ".join(changes))
            counts["updated"] += 1
            if apply:
                row.criteria = criteria
                row.active = True
                if force:
                    row.name = spec["name"]
                    row.description = spec["description"]
                    row.tone = spec["tone"]
                    row.scope = spec["scope"]
                    row.semantic = spec["semantic"]
        if apply:
            session.commit()
    finally:
        session.close()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the global loot-leader badges.")
    ap.add_argument("--apply", action="store_true",
                    help="write the rows (default is a dry run)")
    ap.add_argument("--force", action="store_true",
                    help="also rewrite name/description/tone/scope from the defaults "
                         "(clobbers admin edits)")
    args = ap.parse_args()

    print(f"[seed_leader_badges] {'' if args.apply else 'DRY-RUN '}seeding "
          f"{len(DEFINITIONS)} badge definition(s)")
    counts = seed(apply=args.apply, force=args.force)
    print(f"[seed_leader_badges] done: {counts}")
    if not args.apply and (counts["created"] or counts["updated"]):
        print("[seed_leader_badges] dry run — re-run with --apply to write")
    elif args.apply:
        print("[seed_leader_badges] awards land on the next badge cycle "
              "(hourly from droptracker-core), or run: "
              "venv/bin/python scripts/evaluate_badges.py --only leaders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
