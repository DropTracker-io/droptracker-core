"""Seed the global 'Loot Sweep (All Content)' event template.

Reads all_content_template.json, RE-VALIDATES every task through the real
``validate_task_payload`` (so unknown items/NPCs are caught, not silently
saved), and — only with ``--commit`` — inserts one global EventTemplate row
(group_id NULL, visibility 'public', kind loot_sweep).

Dry-run by default. This is a PROD WRITE; run it deliberately after reviewing
REVIEW.md and fixing the flagged names.

    python -m scripts.loot_sweep.seed_template            # dry-run (validate only)
    python -m scripts.loot_sweep.seed_template --commit   # insert the template
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_NAME = "Loot Sweep (All Content)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="actually insert (prod write)")
    args = ap.parse_args()

    payload = json.load(open(os.path.join(HERE, "all_content_template.json")))
    tasks = payload.get("tasks") or []

    from db.models import EventTemplate, EVENT_TEMPLATE_SCHEMA_VERSION
    from web_api.common import db_session
    from web_api.routes.event_task_validation import validate_task_payload

    failures = []
    with db_session() as s:
        for i, t in enumerate(tasks):
            try:
                validate_task_payload(s, {
                    "type": t.get("type"),
                    "target": t.get("target"),
                    "target_value": t.get("target_value"),
                    "config": t.get("config"),
                })
            except Exception as e:  # ProblemException etc.
                detail = getattr(e, "detail", None) or str(e)
                failures.append((t.get("label"), detail))

        print(f"tasks: {len(tasks)}   validation failures: {len(failures)}")
        for label, detail in failures:
            print(f"  FAIL  {label}: {detail}")

        if failures:
            print("\nFix the flagged names (see REVIEW.md), regenerate, and re-run.")
            return 1
        if not args.commit:
            print("\nDry-run OK. Re-run with --commit to insert the global template.")
            return 0

        existing = (s.query(EventTemplate)
                    .filter(EventTemplate.name == TEMPLATE_NAME,
                            EventTemplate.group_id.is_(None))
                    .first())
        if existing:
            print(f"A global template named {TEMPLATE_NAME!r} already exists "
                  f"(id={existing.id}); aborting to avoid a duplicate.")
            return 1

        row = EventTemplate(
            name=TEMPLATE_NAME,
            description=payload["event"].get("description"),
            source_event_id=None,
            group_id=None,              # global
            created_by_user_id=None,    # system
            visibility="public",
            mode="standard",
            has_bingo=False,
            board_size=5,
            task_count=len(tasks),
            team_count=0,
            schema_version=EVENT_TEMPLATE_SCHEMA_VERSION,
            times_used=0,
            payload=json.dumps(payload),
            active=True,
        )
        s.add(row)
        s.commit()
        print(f"Inserted global template {TEMPLATE_NAME!r} id={row.id} "
              f"with {len(tasks)} loot_sweep tasks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
