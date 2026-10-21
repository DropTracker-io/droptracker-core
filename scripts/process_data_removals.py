"""Erase data-removal requests whose grace period is over, and expire exports.

Run hourly by droptracker-data-removal.timer. For each ``scheduled`` request
past its ``purge_after``: write the private JSON export, anonymize the bot's
Discord posts about a player, erase the rows (``services/data_removal.py``).
Then delete exports older than their 7-day retention.

Dry-run by default; ``--apply`` to write. Runs as ``user``, which owns the
screenshot tree it deletes from and the export directory.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.process_data_removals [--apply]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from db.models.base import Session  # noqa: E402
from services import data_removal as dr  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    s = Session()
    try:
        results = dr.run_due(s, apply=args.apply, bot_token=os.getenv("BOT_TOKEN"))
        for r in results:
            print(json.dumps(r, default=str))
        expired = dr.expire_exports(s, apply=args.apply)
        print(f"due requests: {len(results)}; exports expired: {len(expired)}"
              + ("" if args.apply else " (dry run)"))
        return 1 if any("error" in r for r in results) else 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
