"""One-time cleanup of web_event_task_library (2026-07-12).

Two problems accumulated in the task-library table:

1. **Broken legacy presets.** Dozens of ``legacy_v1`` seeds carry item names
   that aren't real in-game items ("Points", "Uniques", "Godsword", …) — they
   were category-style BoardGame tasks that were always verified manually.
   Copying one into an event 422s in ``validate_task_payload`` ("Unknown
   item(s)"), which surfaced in the picker as an opaque error. These are
   converted to ``type='custom'`` (free-form, manually-awarded) with their
   goal config cleared, which is exactly how they were used in legacy events.

2. **Requirement duplicates.** Until the save-path dedupe landed
   (``web_api/routes/events.py save_task_to_library``), copying a library
   preset into an event re-saved it as a new *public* group row, so the global
   picker showed the same task once per copying clan. For every group-saved
   public row whose requirements (type/target/target_value/canonical config)
   match an older public row: same name -> deactivated (pure copy), different
   name -> demoted to a private, group-only preset. Curated rows are never
   touched — they're managed from /admin/task-library.

Dry-run by default; pass --apply to write.

Run: cd /store/droptracker/disc && venv/bin/python -m scripts.cleanup_event_task_library [--apply]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models.base import session as s  # noqa: E402
from db.models import EventTaskLibraryItem  # noqa: E402
from web_api.common import ProblemException  # noqa: E402
from web_api.routes.event_task_validation import validate_task_payload  # noqa: E402
from web_api.routes.events import _canonical_config  # noqa: E402


def _validation_failure(row: EventTaskLibraryItem) -> str | None:
    """The 422 detail a copy-in of this preset would raise, or None if it
    validates (same code path as POST /events/{id}/tasks)."""
    if row.type == "custom":
        return None
    try:
        validate_task_payload(s, {
            "type": row.type,
            "target": row.target,
            "target_value": row.target_value,
            "config": row.config,
        })
        return None
    except ProblemException as exc:
        return exc.detail or exc.title
    except Exception as exc:  # defensive: garbage config etc.
        return str(exc)


def _signature(row: EventTaskLibraryItem) -> tuple:
    return (
        row.type,
        (row.target or "").strip().lower(),
        row.target_value,
        _canonical_config(row.config),
    )


def _rank(row: EventTaskLibraryItem) -> tuple:
    """Duplicate-keeper precedence: curated/site-wide rows first, then oldest."""
    return (0 if row.group_id is None else 1, row.id)


def main() -> None:
    apply = "--apply" in sys.argv

    rows = (
        s.query(EventTaskLibraryItem)
        .filter(EventTaskLibraryItem.active.is_(True))
        .order_by(EventTaskLibraryItem.id.asc())
        .all()
    )

    # -- Pass 1: broken presets -> custom (manual) tasks ----------------------
    converted = 0
    for row in rows:
        reason = _validation_failure(row)
        if reason is None:
            continue
        print(f"[convert] #{row.id} {row.name!r} ({row.source}) -> custom: {reason}")
        row.type = "custom"
        row.config = None
        converted += 1

    # -- Pass 2: requirement duplicates among group-saved public rows ---------
    by_sig: dict[tuple, list[EventTaskLibraryItem]] = {}
    for row in rows:
        sig = _signature(row)
        if row.type == "custom":
            # Free-form tasks are only "the same" when the name matches too.
            sig = sig + ((row.name or "").strip().lower(),)
        by_sig.setdefault(sig, []).append(row)

    deactivated = demoted = 0
    for sig, group in by_sig.items():
        public = sorted(
            (r for r in group if (r.visibility or "public") == "public"),
            key=_rank,
        )
        if len(public) < 2:
            continue
        keeper = public[0]
        for i, row in enumerate(public[1:], start=1):
            if row.group_id is None:
                # Curated duplicate of a curated row — a data-curation call,
                # not ours to make here.
                print(f"[skip]    #{row.id} {row.name!r} duplicates curated #{keeper.id} "
                      f"{keeper.name!r} — both curated, leaving alone")
                continue
            same_name = next(
                (r for r in public[:i]
                 if r.name.strip().lower() == row.name.strip().lower()),
                None,
            )
            if same_name is not None:
                print(f"[deactivate] #{row.id} {row.name!r} (group {row.group_id}) — "
                      f"copy of #{same_name.id} ({same_name.source})")
                row.active = False
                deactivated += 1
            else:
                print(f"[demote]  #{row.id} {row.name!r} (group {row.group_id}) -> private — "
                      f"same requirements as #{keeper.id} {keeper.name!r}")
                row.visibility = "private"
                demoted += 1

    print(f"\n{converted} converted to custom, {deactivated} deactivated, {demoted} demoted"
          + ("" if apply else "  [dry-run — pass --apply to write]"))
    if apply:
        s.commit()
        print("Committed.")
    else:
        s.rollback()


if __name__ == "__main__":
    main()
