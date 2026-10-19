"""Rebuild the PvP standings in Redis from the database.

The ``pvp:`` keys (``services/pvp_standings.py``) are a cache of
``pvp_kills`` / ``pvp_kill_groups`` and the ``player_deaths`` rows whose
killer was a player. Intake keeps them current; run this after a Redis
flush, after hiding kills in bulk, or once after the first deploy so the PvP
deaths recorded before PvP tracking existed show up on the boards.

Usage
-----
    # dry run (default): counts what would be written, touches nothing
    ./venv/bin/python scripts/rebuild_pvp_standings.py

    # replace every pvp:* key with a fresh build
    ./venv/bin/python scripts/rebuild_pvp_standings.py --apply
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write to Redis (default: dry run)")
    args = ap.parse_args()

    from db.models import Session

    with Session() as session:
        if not args.apply:
            kills = session.execute(text(
                "SELECT COUNT(*) FROM pvp_kills WHERE hidden = 0 AND excluded_reason IS NULL"
            )).scalar()
            counted = session.execute(text(
                "SELECT COUNT(*) FROM pvp_kill_groups WHERE counted = 1"
            )).scalar()
            deaths = session.execute(text(
                "SELECT COUNT(*) FROM player_deaths WHERE killer_type = 'player' "
                "AND world_type = 'main'"
            )).scalar()
            print(f"Would rebuild from {kills} countable kills ({counted} group rows) and "
                  f"up to {deaths} PvP deaths. Re-run with --apply to write.")
            return 0

        from services.pvp_standings import rebuild

        rebuild(session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
