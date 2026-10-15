"""Pay clan points for pets members owned before DropTracker saw them.

For every group with ``points_pet_prior_owned`` on (Points page, "Pets owned
before DropTracker"), pays the new-pet award once for each pet a current
member owns according to their stored collection log or DropTracker's pet
records and was never paid for in that group. The logic lives in
``data/submissions/pet_points.sweep_prior_owned_pets``; this is the hourly
catch-up for members who join or sync their collection log after the option
was turned on (turning it on runs the same sweep immediately).

Dry run (default):  venv/bin/python -m scripts.award_prior_owned_pets
Write:              venv/bin/python -m scripts.award_prior_owned_pets --apply
One group:          ... --group 190
Scheduled by droptracker-prior-owned-pets.timer (hourly).
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _enabled_groups(session) -> list[int]:
    from sqlalchemy import text

    from data.submissions.pet_points import PRIOR_OWNED_KEY

    rows = session.execute(
        text("SELECT DISTINCT group_id FROM group_configurations "
             "WHERE config_key = :k AND config_value = '1'"),
        {"k": PRIOR_OWNED_KEY},
    ).fetchall()
    return sorted(int(r[0]) for r in rows)


async def run(apply: bool, only_group: int | None) -> int:
    from db.models.base import Session

    from data.submissions.pet_points import sweep_prior_owned_pets

    session = Session()
    total_awards = total_points = 0
    try:
        groups = _enabled_groups(session)
        if only_group is not None:
            groups = [g for g in groups if g == only_group]
        for group_id in groups:
            try:
                result = await sweep_prior_owned_pets(session, group_id, apply=apply)
                if apply:
                    session.commit()
                else:
                    session.rollback()
            except Exception as e:
                session.rollback()
                print(f"group {group_id}: sweep failed: {e}")
                continue
            if result.awards:
                verb = "paid" if apply else "would pay"
                print(f"group {group_id}: {verb} {result.points} pts "
                      f"({result.awards} pets, {result.members} members)")
            total_awards += result.awards
            total_points += result.points
        print(f"{'applied' if apply else 'dry run'}: {len(groups)} groups, "
              f"{total_awards} awards, {total_points} points")
        return 0
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the awards")
    parser.add_argument("--group", type=int, default=None, help="only this group id")
    args = parser.parse_args()
    return asyncio.run(run(args.apply, args.group))


if __name__ == "__main__":
    sys.exit(main())
