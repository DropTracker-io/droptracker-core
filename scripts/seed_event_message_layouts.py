"""Seed web_event_message_layouts with the system default layouts (group 1).

The Components-V2 analogue of the group_embeds template rows: one layout per
event message type on the template group (group 1), copied from
services/event_message_layouts.py DEFAULT_LAYOUTS. Groups without their own
row (or without the custom_embeds entitlement) fall back to these at send
time, and the code default backs everything if a row goes missing — so this
seed is about making the defaults *visible and editable in the database*
(the future web layout editor edits these rows), not about correctness.

Idempotent: existing (group 1, message_type) rows are left untouched unless
--force, which rewrites them from the code defaults (clobbers staff edits —
same caveat as scripts/update_docs_pages.py). ``--types a,b`` limits the run to
specific message types, so a layout change can be pushed to just the rows it
touched without rewriting (and clobbering) every other template row.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.seed_event_message_layouts [--force] [--types t1,t2]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models.base import session as _default_session  # noqa: E402
from db.models import EventMessageLayout, EVENT_MESSAGE_LAYOUT_TYPES  # noqa: E402
from services.event_message_layouts import (  # noqa: E402
    DEFAULT_LAYOUTS,
    LAYOUT_SCHEMA_VERSION,
    TEMPLATE_GROUP_ID,
)


def seed(session=None, force: bool = False, types=None) -> dict:
    session = session or _default_session
    only = set(types) if types else None
    created, updated, skipped = 0, 0, 0
    existing = {
        row.message_type: row
        for row in session.query(EventMessageLayout)
        # event_id 0 = the group-level template rows; global events' per-event
        # overrides also live on group 1 (web66a) and must not be clobbered.
        .filter(EventMessageLayout.group_id == TEMPLATE_GROUP_ID,
                EventMessageLayout.event_id == 0)
        .all()
    }
    for message_type in EVENT_MESSAGE_LAYOUT_TYPES:
        if only is not None and message_type not in only:
            continue
        default = DEFAULT_LAYOUTS.get(message_type)
        if not default:
            continue
        accent = default.get("accent_color")
        payload = json.dumps({"blocks": default["blocks"]})
        row = existing.get(message_type)
        if row is None:
            session.add(EventMessageLayout(
                group_id=TEMPLATE_GROUP_ID,
                message_type=message_type,
                accent_color=accent,
                layout=payload,
                schema_version=LAYOUT_SCHEMA_VERSION,
            ))
            created += 1
        elif force:
            row.accent_color = accent
            row.layout = payload
            row.schema_version = LAYOUT_SCHEMA_VERSION
            updated += 1
        else:
            skipped += 1
    session.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


if __name__ == "__main__":
    force = "--force" in sys.argv
    types = None
    for arg in sys.argv[1:]:
        if arg.startswith("--types="):
            types = [t.strip() for t in arg.split("=", 1)[1].split(",") if t.strip()]
        elif arg == "--types":
            idx = sys.argv.index(arg)
            if idx + 1 < len(sys.argv):
                types = [t.strip() for t in sys.argv[idx + 1].split(",") if t.strip()]
    result = seed(force=force, types=types)
    scope = f" [{', '.join(types)}]" if types else ""
    print(
        f"Seeded event message layouts (group {TEMPLATE_GROUP_ID}){scope}: "
        f"{result['created']} created, {result['updated']} updated, "
        f"{result['skipped']} left untouched."
    )
