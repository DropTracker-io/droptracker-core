"""Restore the capitalisation WOM had all along on stored player names.

``utils.wiseoldman.check_user_by_username``/``check_user_by_id`` returned the
wom library's ``Player.username``, documented as "always lowercase", as the
name their callers bind to ``canonical_name`` and write to
``Player.player_name`` (db/ops.create_player,
data/submissions/common.ensure_player_and_auth). Their docstrings claimed the
value was the displayName; it never was. So every row created through a WOM
lookup was stored with its capitals stripped: 11,940 of 22,267 rows on
2026-08-30, 11,915 of which carry a wom_id.

Worse, none of them could ever recover. Every rename guard in the codebase asks
``normalize_player_display_equivalence(old) != normalize_player_display_equivalence(new)``
before writing, and that lowercases both sides — so a name that differed *only*
in case was, by construction, never an update. Casing could be set at row
creation and never again.

The lookup helpers now return the displayName and those guards have a
casing-repair branch, which fixes new and re-resolved rows. This script is for
the corpus already on disk.

It only ever applies ``utils.format.prefer_display_casing``, which returns a
name only when it is the same string letter-for-letter and separator-for-
separator, differing purely in case, and the stored one has no capitals while
WOM's does. So a row can gain capitals and nothing else: no letter changes, no
separator changes, no renames. Every lookup in the codebase compares through
``normalize_player_display_equivalence`` or SQL ``LOWER``/``ilike``, so the
rewritten name resolves exactly as the old one did, and no ``name_change``
notification is raised (this is not a rename).

Two passes, cheapest first:

  * ``groups`` — one WOM call per tracked group returns the whole roster with a
    full player object each, covering 4,582 of the candidates for 280 calls.
  * ``players`` — one call per remaining candidate. ~7.3k accounts against a
    shared 100-per-65s budget is roughly 80 minutes, so ``--limit`` bounds a
    run and the script is idempotent: re-run it until it reports no changes.

Both passes share the Redis-backed WOM rate limiter with every other process on
the box, so this is safe to run while the bots are up.

Dry-run by default; ``--apply`` is required to write anything.

    cd /store/droptracker/disc && venv/bin/python -m scripts.backfill_player_name_casing
    cd /store/droptracker/disc && venv/bin/python -m scripts.backfill_player_name_casing --apply
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from db.models.base import session  # noqa: E402
from utils.format import prefer_display_casing  # noqa: E402

# Rows whose name is entirely lowercase yet contains a letter to capitalise.
# utf8mb4_bin forces a case-sensitive compare; the table's own collation is
# case-insensitive, which would make this predicate match everything.
CANDIDATE_SQL = """
    SELECT player_id, player_name, wom_id
    FROM players
    WHERE player_name COLLATE utf8mb4_bin = LOWER(player_name)
      AND player_name REGEXP '[a-z]'
      AND wom_id IS NOT NULL AND wom_id > 0
"""


def load_candidates():
    """{wom_id: (player_id, stored_name)} for every row that could gain capitals."""
    rows = session.execute(text(CANDIDATE_SQL)).fetchall()
    return {int(r[2]): (int(r[0]), r[1]) for r in rows}


def tracked_group_wom_ids():
    rows = session.execute(text(
        "SELECT wom_id FROM groups WHERE wom_id IS NOT NULL AND wom_id > 0"
    )).fetchall()
    return [int(r[0]) for r in rows]


async def _roster_display_names(group_wom_id):
    """{wom_id: displayName} for one WOM group, or {} if the call fails."""
    from utils.wiseoldman import client, limiter

    if not await limiter.wait():
        return {}
    await client.start()
    result = await client.groups.get_details(group_wom_id)
    if not getattr(result, "is_ok", False):
        return {}
    group = result.unwrap()
    names = {}
    for membership in getattr(group, "memberships", None) or []:
        player = getattr(membership, "player", None)
        display_name = getattr(player, "display_name", None)
        player_wom_id = getattr(player, "id", None)
        if display_name and player_wom_id:
            names[int(player_wom_id)] = str(display_name)
    return names


async def _player_display_name(wom_id):
    """displayName for one WOM player id, or None if the call fails."""
    from utils.wiseoldman import client, limiter, _wom_display_name

    if not await limiter.wait():
        return None
    await client.start()
    result = await client.players.get_details_by_id(player_id=wom_id)
    if not getattr(result, "is_ok", False):
        return None
    player = result.unwrap()
    return _wom_display_name(player) if player is not None else None


def _record(fixes, candidates, wom_id, display_name):
    """Queue a rename if prefer_display_casing sanctions it. True if queued."""
    entry = candidates.get(wom_id)
    if entry is None or not display_name:
        return False
    player_id, stored_name = entry
    better_name = prefer_display_casing(stored_name, display_name)
    if not better_name:
        return False
    fixes.append((player_id, stored_name, better_name))
    return True


def _commit(fixes, apply_changes):
    if not apply_changes or not fixes:
        return
    for player_id, _old, new_name in fixes:
        session.execute(
            text("UPDATE players SET player_name = :name WHERE player_id = :pid"),
            {"name": new_name, "pid": player_id},
        )
    session.commit()


async def run(apply_changes, limit, skip_groups, skip_players):
    candidates = load_candidates()
    print(f"{len(candidates)} rows are all-lowercase with a WOM id.\n")
    fixes = []
    seen = set()

    if not skip_groups:
        group_ids = tracked_group_wom_ids()
        print(f"Pass 1/2 — group rosters ({len(group_ids)} calls):")
        for i, group_wom_id in enumerate(group_ids, 1):
            try:
                roster = await _roster_display_names(group_wom_id)
            except Exception as exc:
                print(f"  group {group_wom_id}: {type(exc).__name__}: {exc}")
                continue
            for wom_id, display_name in roster.items():
                if wom_id in seen:
                    continue
                seen.add(wom_id)
                _record(fixes, candidates, wom_id, display_name)
            if i % 25 == 0 or i == len(group_ids):
                print(f"  {i}/{len(group_ids)} groups, {len(fixes)} names to fix")
        _commit(fixes, apply_changes)
        print(f"  pass 1 found {len(fixes)}\n")

    if not skip_players:
        remaining = [w for w in candidates if w not in seen]
        if limit:
            remaining = remaining[:limit]
        print(f"Pass 2/2 — per-player ({len(remaining)} calls):")
        before = len(fixes)
        pending = []
        for i, wom_id in enumerate(remaining, 1):
            try:
                display_name = await _player_display_name(wom_id)
            except Exception as exc:
                print(f"  wom_id {wom_id}: {type(exc).__name__}: {exc}")
                continue
            if _record(fixes, candidates, wom_id, display_name):
                pending.append(fixes[-1])
            # Commit as we go: this pass runs for over an hour and is meant to
            # be interruptible without losing the calls already spent.
            if apply_changes and len(pending) >= 50:
                _commit(pending, apply_changes)
                pending = []
            if i % 100 == 0 or i == len(remaining):
                print(f"  {i}/{len(remaining)} players, {len(fixes) - before} names to fix")
        _commit(pending, apply_changes)
        print(f"  pass 2 found {len(fixes) - before}\n")

    print(f"{len(fixes)} names to correct.")
    for _pid, old, new in fixes[:25]:
        print(f"   {old!r} -> {new!r}")
    if len(fixes) > 25:
        print(f"   ... and {len(fixes) - 25} more")
    if not apply_changes:
        print("\nDry run — nothing was changed. Re-run with --apply to act.")
    elif fixes:
        print(f"\nApplied {len(fixes)} capitalisation fixes.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the corrected names (default: dry run)")
    parser.add_argument("--limit", type=int, default=0,
                        help="cap the per-player pass; the script is idempotent, so re-run to continue")
    parser.add_argument("--skip-groups", action="store_true",
                        help="skip the cheap group-roster pass")
    parser.add_argument("--skip-players", action="store_true",
                        help="run only the group-roster pass (280 calls, no long tail)")
    args = parser.parse_args()

    async def _main():
        try:
            return await run(args.apply, args.limit, args.skip_groups, args.skip_players)
        finally:
            # Nothing else in this process will reuse the WOM client, and
            # leaving it open makes aiohttp scream about it on the way out.
            from utils.wiseoldman import client
            try:
                await client.close()
            except Exception:
                pass

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
