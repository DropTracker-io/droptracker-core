"""Seed or regenerate the plugin manifest sections.

Dry-run by default; ``--apply`` writes. Idempotent: re-running with no upstream
change reports "no changes" and touches nothing, so it is safe to wire into a
scheduled job later.

    ./venv/bin/python -m scripts.build_manifest              # show what would change
    ./venv/bin/python -m scripts.build_manifest --apply      # write it
    ./venv/bin/python -m scripts.build_manifest --show       # print the served manifest

Sections default to the values in ``services/plugin_manifest.DEFAULT_SECTIONS``.
This script exists so those defaults can be pushed into the database (where they
become editable without a deploy) and so future generated sections — the quest
id list and the collection log structure, both of which want extracting from the
game cache — have an obvious home.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from db.models import PluginManifestSection, Session
from services.plugin_manifest import CACHE_KEY, DEFAULT_SECTIONS, manifest_payload

SOURCE = "scripts/build_manifest.py"


def _bust_cache() -> None:
    """Drop the cached manifest so a write is visible to new sessions at once.

    Best-effort: the cache expires on its own, so a Redis problem must not fail
    a run that already committed.
    """
    try:
        from utils.redis import redis_client

        redis_client.delete(CACHE_KEY)
        print(f"Invalidated cache key {CACHE_KEY}")
    except Exception as exc:
        print(f"Could not invalidate {CACHE_KEY} ({exc}); it expires on its own")


def _load_generated(key):
    """Generated payloads that are too large to keep inline in DEFAULT_SECTIONS.

    The combat achievement registry is ~650 entries; it lives beside this
    script so regenerating it after a game update is a file swap rather than a
    code edit. Do regenerate it: the registry is what the profile's achievement
    browser counts against, and between an update and a rebuild every task
    added by that update is invisible there.
    """
    if key != "combat_achievement_tasks":
        return None
    # Extracted from the game cache by scripts/cache/extract-ca-tasks.mjs, which
    # is the only source carrying the varp/bit each task lives at — the join
    # that turns stored varps into named tasks.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ca_tasks.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"Could not read {path} ({exc}); leaving {key} empty")
        return None


def _canonical(payload) -> str:
    """Stable JSON so an unchanged section compares equal across runs."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build(apply: bool) -> int:
    session = Session()
    try:
        existing = {row.key: row for row in session.query(PluginManifestSection).all()}

        created, updated, unchanged = [], [], []
        for key, spec in DEFAULT_SECTIONS.items():
            generated = _load_generated(key)
            wanted = _canonical(generated if generated is not None else spec["payload"])
            row = existing.get(key)
            if row is None:
                created.append(key)
                if apply:
                    session.add(
                        PluginManifestSection(
                            key=key,
                            payload=wanted,
                            description=spec.get("description"),
                            source=SOURCE,
                        )
                    )
                continue

            # Never clobber a hand-edited row: the whole point of the table is
            # that someone can fix a value at runtime, and a later run of this
            # script must not silently revert them.
            if row.source and row.source != SOURCE:
                unchanged.append(f"{key} (hand-edited, left alone)")
                continue

            if _canonical(json.loads(row.payload)) == wanted:
                unchanged.append(key)
                continue

            updated.append(key)
            if apply:
                row.payload = wanted
                row.description = spec.get("description")
                row.source = SOURCE

        if apply:
            session.commit()
            if created or updated:
                _bust_cache()

        verb = "Wrote" if apply else "Would write"
        print(f"{verb}: {len(created)} created, {len(updated)} updated, {len(unchanged)} unchanged")
        for key in created:
            print(f"  + {key}")
        for key in updated:
            print(f"  ~ {key}")
        for key in unchanged:
            print(f"  = {key}")
        if not apply and (created or updated):
            print("\nDry run — re-run with --apply to write.")
        return 0
    finally:
        session.close()


def show() -> int:
    session = Session()
    try:
        rows = session.query(PluginManifestSection).all()
        print(json.dumps(manifest_payload(rows), indent=2, sort_keys=True))
        return 0
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--show", action="store_true", help="print the manifest as served")
    args = parser.parse_args()

    if args.show:
        return show()
    return build(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
