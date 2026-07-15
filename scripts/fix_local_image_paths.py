"""One-time remediation: rewrite filesystem image_url values to public URLs.

Several submission processors historically stored ``download_player_image``'s
LOCAL path (``/store/droptracker/disc/static/assets/img/user-upload/...``)
instead of its external URL on the row's ``image_url`` — so Discord embeds,
the site and the HOF rendered nothing for those entries. The files themselves
downloaded fine and still exist; the fix is a pure prefix swap to the URL the
same directory is served at (nginx: /img/user-upload).

Affected (2026-07-15 audit): personal_best (bug LIVE until data/submissions/
pb.py was fixed the same day), collection + combat_achievement (historical,
writers fixed 2025-08). player_pets/drops carry no such rows.

Dry-run by default; ``--apply`` performs the UPDATEs. Either way a snapshot of
every affected (table, id, old image_url) is written to
``logs/local_image_paths_<ts>.tsv`` so the change is reversible.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.fix_local_image_paths [--apply]
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db.models.base import session  # noqa: E402

LOCAL_PREFIX = "/store/droptracker/disc/static/assets/img/user-upload/"
URL_PREFIX = "https://www.droptracker.io/img/user-upload/"

# table -> primary key column (collection's PK is log_id, not id)
TABLES = {"personal_best": "id", "collection": "log_id", "combat_achievement": "id"}


def main() -> None:
    apply = "--apply" in sys.argv
    ts = time.strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", f"local_image_paths_{ts}.tsv",
    )
    os.makedirs(os.path.dirname(snapshot_path), exist_ok=True)

    total = 0
    with open(snapshot_path, "w", encoding="utf-8") as snap:
        snap.write("table\tid\told_image_url\n")
        for table, pk in TABLES.items():
            rows = session.execute(text(
                f"SELECT {pk}, image_url FROM {table} "
                "WHERE image_url LIKE :pfx"
            ), {"pfx": LOCAL_PREFIX + "%"}).all()
            for rid, old in rows:
                snap.write(f"{table}\t{rid}\t{old}\n")
            print(f"{table}: {len(rows)} rows with local paths")
            total += len(rows)
            if apply and rows:
                result = session.execute(text(
                    f"UPDATE {table} "
                    "SET image_url = CONCAT(:url, SUBSTRING(image_url, :cut)) "
                    "WHERE image_url LIKE :pfx"
                ), {
                    "url": URL_PREFIX,
                    "cut": len(LOCAL_PREFIX) + 1,
                    "pfx": LOCAL_PREFIX + "%",
                })
                session.commit()
                print(f"{table}: rewrote {result.rowcount} rows")

    print(f"\nSnapshot: {snapshot_path} ({total} rows)")
    if not apply:
        print("Dry run — re-run with --apply to rewrite.")


if __name__ == "__main__":
    main()
