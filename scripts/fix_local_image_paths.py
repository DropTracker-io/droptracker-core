"""One-time remediation: rewrite filesystem image_url values to public URLs.

Several submission processors historically stored ``download_player_image``'s
LOCAL path (``/store/droptracker/disc/static/assets/img/user-upload/...``)
instead of its external URL on the row's ``image_url`` — so Discord embeds,
the site and the HOF rendered nothing for those entries. The files themselves
downloaded fine and still exist; the fix is a pure prefix swap to the URL the
same directory is served at (nginx: /img/user-upload).

Affected (2026-07-15 audit): personal_best (bug LIVE until data/submissions/
pb.py was fixed the same day), collection + combat_achievement (historical,
writers fixed 2025-08). player_pets has no image_url column.

Affected (2026-08-01): drops, via ``POST /manual-submit``'s multipart branch —
the only caller that uploads a file there is the Discord ``/submit`` command
(shipped 2026-07-29; the website form uploads to B2 instead), so the window is
small. Fixed in api/routes/webhook.py the same day.

``drops`` holds ~180M rows, so an unbounded ``LIKE`` scan on it just times the
connection out. It is scanned backwards from MAX(drop_id) in PK chunks over
``--scan-ids`` ids (default 10M, ~3 weeks of volume — comfortably past when
the bug could have written a row) and rewritten by explicit id.

Dry-run by default; ``--apply`` performs the UPDATEs. Either way a snapshot of
every affected (table, id, old image_url) is written to
``logs/local_image_paths_<ts>.tsv`` so the change is reversible.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.fix_local_image_paths [--apply] [--scan-ids N]
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text  # noqa: E402

from db.models.base import session  # noqa: E402

LOCAL_PREFIX = "/store/droptracker/disc/static/assets/img/user-upload/"
URL_PREFIX = "https://www.droptracker.io/img/user-upload/"

# table -> primary key column (collection's PK is log_id, not id)
TABLES = {"personal_best": "id", "collection": "log_id", "combat_achievement": "id"}

# drops is scanned separately — see the module docstring.
DROPS_CHUNK = 250_000
DROPS_DEFAULT_SCAN_IDS = 10_000_000


def _scan_ids_arg() -> int:
    if "--scan-ids" in sys.argv:
        return int(sys.argv[sys.argv.index("--scan-ids") + 1])
    return DROPS_DEFAULT_SCAN_IDS


def find_drops_rows(scan_ids: int) -> list[tuple[int, str]]:
    """Chunked PK sweep of the tail of ``drops`` for filesystem image_urls."""
    max_id = session.execute(text("SELECT MAX(drop_id) FROM drops")).scalar() or 0
    lo = max(0, max_id - scan_ids)
    found: list[tuple[int, str]] = []
    while lo < max_id:
        hi = lo + DROPS_CHUNK
        found.extend(session.execute(text(
            "SELECT drop_id, image_url FROM drops "
            "WHERE drop_id > :lo AND drop_id <= :hi AND image_url LIKE :pfx"
        ), {"lo": lo, "hi": hi, "pfx": LOCAL_PREFIX + "%"}).all())
        lo = hi
    return found


def main() -> None:
    apply = "--apply" in sys.argv
    scan_ids = _scan_ids_arg()
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

        drop_rows = find_drops_rows(scan_ids)
        for rid, old in drop_rows:
            snap.write(f"drops\t{rid}\t{old}\n")
        print(f"drops: {len(drop_rows)} rows with local paths (last {scan_ids} ids)")
        total += len(drop_rows)
        if apply and drop_rows:
            # By id, not by LIKE: the predicate that found these can't be
            # re-run against the whole table without timing out.
            result = session.execute(text(
                "UPDATE drops "
                "SET image_url = CONCAT(:url, SUBSTRING(image_url, :cut)) "
                "WHERE drop_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)), {
                "url": URL_PREFIX,
                "cut": len(LOCAL_PREFIX) + 1,
                "ids": [rid for rid, _ in drop_rows],
            })
            session.commit()
            print(f"drops: rewrote {result.rowcount} rows")

    print(f"\nSnapshot: {snapshot_path} ({total} rows)")
    if not apply:
        print("Dry run — re-run with --apply to rewrite.")


if __name__ == "__main__":
    main()
