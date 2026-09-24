"""Tidy the public event task library (docs/TASK_GENERATOR_PLAN.md, step 4).

Dry-run by default; ``--apply`` writes. Idempotent. Only ever soft-deletes
(``active = 0``), so every change is reversible from /admin/task-library.

Retires two kinds of PUBLIC preset:

1. **Duplicates** — several active public rows with the same name
   (case-insensitive). Keeps one per name, preferring a row the engine can
   score automatically over a manual-only "custom" one, then curated >
   legacy > group-shared, then the oldest id.
2. **Broken presets** — rows whose requirements no longer pass the task
   validator (a renamed item, a retired NPC). Copying one into an event
   already fails; the bulk picker skips it silently, so they only clutter the
   list.

Also REPORTS, without touching, presets that look like placeholders
(manual-only "custom" rows imported from the legacy board game) so an admin
can decide on them by hand.

    venv/bin/python -m scripts.tidy_task_library          # report
    venv/bin/python -m scripts.tidy_task_library --apply  # deactivate
"""
from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")

_SOURCE_RANK = {"curated": 0, "legacy_v1": 1, "group": 2}


def plan(rows, validate) -> dict:
    """Pure decision core: which rows to retire and why.

    ``rows`` are dicts with id/name/source/type/target/target_value/config;
    ``validate(row) -> bool`` says whether the requirements still validate.
    """
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(r["name"].strip().lower(), []).append(r)
    duplicates: list[tuple[dict, dict]] = []
    keep_ids: set[int] = set()
    for group in by_name.values():
        group.sort(key=lambda r: (r["type"] == "custom",
                                  _SOURCE_RANK.get(r["source"], 9), r["id"]))
        keeper = group[0]
        keep_ids.add(keeper["id"])
        duplicates.extend((r, keeper) for r in group[1:])
    dup_ids = {r["id"] for r, _ in duplicates}
    broken = [r for r in rows if r["id"] not in dup_ids and not validate(r)]
    placeholders = [
        r for r in rows
        if r["id"] not in dup_ids and r not in broken
        and r["type"] == "custom" and r["source"] == "legacy_v1"
    ]
    return {"duplicates": duplicates, "broken": broken, "placeholders": placeholders}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="deactivate the retired rows")
    args = ap.parse_args()

    from db import AuditLog, EventTaskLibraryItem
    from db.models.base import Session
    from web_api.common import ProblemException
    from web_api.routes.event_task_validation import validate_task_payload

    s = Session()
    try:
        rows_orm = (s.query(EventTaskLibraryItem)
                    .filter(EventTaskLibraryItem.active.is_(True),
                            EventTaskLibraryItem.visibility == "public")
                    .all())
        rows = [{"id": r.id, "name": r.name or "", "source": r.source, "type": r.type,
                 "target": r.target, "target_value": r.target_value, "config": r.config}
                for r in rows_orm]

        def _valid(r) -> bool:
            if r["type"] == "custom":
                return True  # manual-award presets have nothing to validate
            try:
                validate_task_payload(s, {"type": r["type"], "target": r["target"],
                                          "target_value": r["target_value"],
                                          "config": r["config"]})
                return True
            except ProblemException:
                return False
            except Exception:
                return True  # unexpected validator error: never retire on it

        result = plan(rows, _valid)
        s.rollback()  # the validator may have touched the session

        print(f"Active public presets: {len(rows)}")
        print(f"\nDuplicates to retire: {len(result['duplicates'])}")
        for r, keeper in result["duplicates"]:
            print(f"  #{r['id']:<5} {r['name']!r} ({r['source']}) -> keeps #{keeper['id']} ({keeper['source']})")
        print(f"\nNo longer valid, to retire: {len(result['broken'])}")
        for r in result["broken"]:
            print(f"  #{r['id']:<5} {r['name']!r} [{r['type']}] target={r['target']!r}")
        print(f"\nLegacy manual-only presets (report only, not changed): {len(result['placeholders'])}")
        for r in result["placeholders"]:
            print(f"  #{r['id']:<5} {r['name']!r}")

        retire = [r for r, _ in result["duplicates"]] + result["broken"]
        if not args.apply:
            print(f"\nDry run. {len(retire)} preset(s) would be deactivated; re-run with --apply.")
            return 0
        if not retire:
            print("\nNothing to do.")
            return 0
        ids = [r["id"] for r in retire]
        (s.query(EventTaskLibraryItem)
         .filter(EventTaskLibraryItem.id.in_(ids))
         .update({EventTaskLibraryItem.active: False}, synchronize_session=False))
        s.add(AuditLog(
            actor_user_id=None,
            action="event.library.tidy",
            target="web_event_task_library",
            after=json.dumps({"deactivated": ids,
                              "duplicates": [r["id"] for r, _ in result["duplicates"]],
                              "broken": [r["id"] for r in result["broken"]]}),
        ))
        s.commit()
        print(f"\nDeactivated {len(ids)} preset(s).")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
