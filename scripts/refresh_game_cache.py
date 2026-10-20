"""Refresh everything we derive from the OSRS game cache, when it changes.

The game cache is the source of truth for items (ids and names) and for the
collection log's layout, and abextm's public mirror publishes each new build
within minutes of a game update. This job checks the mirror's newest commit
and, only when it differs from what we last built from, regenerates:

* ``scripts/cache/item_catalogue.live.json`` — every item's id, name and
  stack variants (``utils/item_catalogue``, read by the site and by
  ``ensure_item_by_name`` to mint items the wiki has no page for yet);
* the collection log structure and its manifest row, via
  ``scripts.sync_collection_log --refresh --apply`` (which keeps its own
  guards against a truncated extract).

Both extractors are pinned to the same mirror commit, so the two can never
describe different game builds. Runs hourly from
``droptracker-game-cache.timer``; a run with nothing new costs one GitHub API
call. Game updates do not reliably land on one weekday (The Fractured Archive
released on a Tuesday), which is why this replaced the weekly clog timer.

Dry run by default:
    venv/bin/python -m scripts.refresh_game_cache            # what would change
    venv/bin/python -m scripts.refresh_game_cache --apply    # do it
    venv/bin/python -m scripts.refresh_game_cache --apply --force   # rebuild anyway
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DISC_DIR = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(SCRIPT_DIR, "cache")
LIVE_CATALOGUE = os.path.join(CACHE_DIR, "item_catalogue.live.json")
TRACKED_CATALOGUE = os.path.join(SCRIPT_DIR, "item_catalogue.json")
LIVE_STRUCTURE = os.path.join(CACHE_DIR, "collection_log_structure.live.json")
MIRROR_API = "https://api.github.com/repos/abextm/osrs-cache/commits/master"

#: A new catalogue naming far fewer items than the last is a broken extract
#: (the mirror mid-push, a decoder that no longer understands the cache), not
#: Jagex deleting items. Refuse it and keep the old one.
MIN_NAMED_RATIO = 0.98


def mirror_head() -> tuple[str, str] | None:
    """``(sha, date)`` of the mirror's newest commit, or None if unreachable."""
    req = urllib.request.Request(MIRROR_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "droptracker-game-cache-refresh",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        return body["sha"], body["commit"]["committer"]["date"]
    except Exception as exc:
        print(f"cache mirror unreachable ({exc}); nothing to do this run")
        return None


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def built_from(path: str) -> str | None:
    data = _read_json(path)
    return (data or {}).get("cache_commit")


def refresh_catalogue(sha: str, apply: bool) -> bool:
    """Regenerate the live item catalogue at ``sha``. True on success."""
    previous = _read_json(LIVE_CATALOGUE) or _read_json(TRACKED_CATALOGUE) or {}
    temp = LIVE_CATALOGUE + ".new"
    print(f"extracting the item catalogue at {sha[:12]}…", flush=True)
    try:
        with open(temp, "w") as out:
            subprocess.run(["node", "extract-item-catalogue.mjs"], cwd=CACHE_DIR,
                           stdout=out, check=True,
                           env={**os.environ, "OSRS_CACHE_COMMIT": sha})
        fresh = _read_json(temp)
        if not fresh or not fresh.get("items"):
            raise RuntimeError("the extractor wrote no items")
    except Exception as exc:
        print(f"item catalogue extract failed: {exc}")
        _unlink(temp)
        return False

    old_items = previous.get("items") or {}
    new_items = fresh["items"]
    old_named = len(old_items)
    if old_named and len(new_items) < old_named * MIN_NAMED_RATIO:
        print(f"refusing the new catalogue: {len(new_items)} named items against "
              f"{old_named} before")
        _unlink(temp)
        return False

    added = sorted((int(k) for k in new_items.keys() - old_items.keys()))
    renamed = [int(k) for k in new_items.keys() & old_items.keys()
               if new_items[k].get("n") != old_items[k].get("n")]
    print(f"item catalogue: {len(new_items)} named ({len(added)} new, "
          f"{len(renamed)} renamed)")
    for item_id in added[:40]:
        print(f"  + {item_id} {new_items[str(item_id)]['n']}")
    if len(added) > 40:
        print(f"  … and {len(added) - 40} more")

    if not apply:
        _unlink(temp)
        return True
    os.chmod(temp, 0o644)  # the API reads it as another user
    os.replace(temp, LIVE_CATALOGUE)
    print(f"wrote {LIVE_CATALOGUE}")
    return True


def refresh_collection_log(sha: str, apply: bool) -> bool:
    # --refresh rewrites the structure file even without --apply, so a dry
    # run extracts into a scratch copy and leaves the live one alone.
    structure = LIVE_STRUCTURE if apply else LIVE_STRUCTURE + ".dryrun"
    cmd = [sys.executable, "-m", "scripts.sync_collection_log", "--refresh",
           "--structure", structure]
    if apply:
        cmd.append("--apply")
    print(f"refreshing the collection log at {sha[:12]}…", flush=True)
    try:
        result = subprocess.run(cmd, cwd=DISC_DIR,
                                env={**os.environ, "OSRS_CACHE_COMMIT": sha})
    finally:
        if not apply:
            _unlink(structure)
    return result.returncode == 0


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if already built from the newest commit")
    args = parser.parse_args(argv)

    head = mirror_head()
    if head is None:
        return 0
    sha, date = head
    print(f"cache mirror is at {sha[:12]} ({date})")

    ok = True
    if args.force or built_from(LIVE_CATALOGUE) != sha:
        ok = refresh_catalogue(sha, args.apply) and ok
    else:
        print("item catalogue already current")
    if args.force or built_from(LIVE_STRUCTURE) != sha:
        ok = refresh_collection_log(sha, args.apply) and ok
    else:
        print("collection log structure already current")
    if not args.apply:
        print("Dry run — re-run with --apply to write.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
