"""Remove recorded collection log rows that are not collection log slots.

Before the log's structure was known, the sync stored whatever a client
reported, so rows exist for items that are not slots at all — coins, ordinary
equipment, and (on the dev box) synthetic test data. They then rendered on the
collection log page, which is how the problem was noticed.

Only rows whose item id appears in **no** page of the current structure are
removed, so a real slot can never be deleted by this.

**Think twice before running it now.** The sync no longer filters what it
stores, precisely because the structure was wrong about a hundred slots and the
filter was destroying the game's correct answer on the way in. Rows the
structure cannot place are therefore the *evidence* that it is wrong — they are
what ``scripts.sync_collection_log --audit`` reads to find the right id, and
what makes a structure correction repair accounts that have already synced.
Delete them and that goes with them. Run the audit first, and only prune what it
agrees is not a slot.

Dry-run by default. Re-run the structure sync first if a game update has added
slots, or this will treat genuinely-new items as unknown:

    ./venv/bin/python -m scripts.sync_collection_log --apply
    ./venv/bin/python -m scripts.prune_unknown_clog_items
    ./venv/bin/python -m scripts.prune_unknown_clog_items --apply
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import text

from db.models import PluginManifestSection, Session


def defined_slot_ids(session) -> set[int]:
    row = (
        session.query(PluginManifestSection)
        .filter(PluginManifestSection.key == "collection_log")
        .first()
    )
    if row is None:
        raise SystemExit(
            "No collection_log structure in the manifest — run "
            "scripts.sync_collection_log --apply first. Refusing to prune "
            "without knowing what a real slot is."
        )
    structure = json.loads(row.payload)
    return {
        item_id
        for tab in structure
        for page in tab.get("pages", [])
        for item_id in page.get("items", [])
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    parser.add_argument("--player", type=int, help="limit to one player id")
    args = parser.parse_args()

    session = Session()
    try:
        defined = defined_slot_ids(session)
        print(f"structure defines {len(defined)} slots")

        where = "WHERE player_id = :p" if args.player else ""
        params = {"p": args.player} if args.player else {}
        rows = session.execute(
            text(f"SELECT player_id, item_id FROM player_clog_items {where}"), params
        ).fetchall()

        doomed = [(pid, iid) for pid, iid in rows if iid not in defined]
        print(f"{len(rows)} recorded rows, {len(doomed)} not a known slot")

        by_player = {}
        for pid, iid in doomed:
            by_player.setdefault(pid, []).append(iid)
        for pid, ids in sorted(by_player.items()):
            preview = ", ".join(str(i) for i in sorted(ids)[:10])
            more = f" (+{len(ids) - 10} more)" if len(ids) > 10 else ""
            print(f"  player {pid}: {len(ids)} — {preview}{more}")

        if not args.apply:
            print("\nDry run — re-run with --apply to delete.")
            return 0

        for pid, ids in by_player.items():
            # Chunked so a player with a very long list cannot build a statement
            # bigger than the server will accept.
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ", ".join(f":i{n}" for n in range(len(chunk)))
                bind = {f"i{n}": v for n, v in enumerate(chunk)}
                bind["p"] = pid
                session.execute(
                    text(
                        "DELETE FROM player_clog_items WHERE player_id = :p "
                        f"AND item_id IN ({placeholders})"
                    ),
                    bind,
                )
        session.commit()
        print(f"deleted {len(doomed)} rows")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
