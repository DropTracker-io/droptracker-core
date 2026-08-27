"""Put the collection log's structure — tabs, pages, slots — into the manifest.

Why this exists: the collection log page could only render a flat grid of every
slot we happened to know about, because nothing told us which slots belong to
which page ("Abyssal Sire", "Barrows Chests", ...) or which tab those pages live
under. That structure is what makes the in-game interface legible, and without
it the page is a wall of icons.

**Source: the game cache.** ``scripts/cache/extract-collection-log.mjs`` reads
the tab and page structs the client itself draws from, and writes them to
``scripts/collection_log_structure.json``; this script turns that into the
manifest section. Refresh the JSON after a game update — ``--refresh`` does it —
and re-run with ``--apply``.

**This used to be scraped from the wiki, and the wiki cannot answer it.** The
article publishes item *names*, and a name does not identify an item. Half the
awkward slots are one of several versions of the same thing — the log holds the
*uncharged* Alchemist's amulet, the *empty* Master scroll book, the *full*
Trident of the Seas — so every id was a guess, checked after the fact against
what real accounts reported. That loop ran for weeks and still left the
structure wrong about three slots (Trident of the Seas (full), the Yama Dossier
and the Alchemist's amulet, held by 57, 55 and 39 accounts) and carrying two ids
no account has ever held.

The cache has no such ambiguity, and it settles the awkward cases outright:
**enum 3721 is the game's own id-replacement map**. A dozen slots are stored
against one id and drawn as another — the Coal bag's page entry is 12019, the
log draws 25627 — and the client remaps through 3721 before drawing, so the
drawn id is what a synced client reports. Getting those right by inference from
a wiki name is not possible; reading them takes one enum.

The result is checkable, and checks out: of the 1716 ids the cache defines,
1715 are held by at least one synced account. The one nobody has is the 3rd age
pickaxe.

    ./venv/bin/python -m scripts.sync_collection_log             # dry run
    ./venv/bin/python -m scripts.sync_collection_log --apply     # write
    ./venv/bin/python -m scripts.sync_collection_log --refresh   # re-read the cache first
    ./venv/bin/python -m scripts.sync_collection_log --audit     # check what is stored
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, List

MANIFEST_KEY = "collection_log"
SOURCE = "scripts/sync_collection_log.py"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STRUCTURE_PATH = os.path.join(SCRIPT_DIR, "collection_log_structure.json")
EXTRACTOR_DIR = os.path.join(SCRIPT_DIR, "cache")
EXTRACTOR = "extract-collection-log.mjs"


def refresh_structure() -> int:
    """Re-run the cache extractor over ``STRUCTURE_PATH``.

    Written to a temporary file and moved into place, so a failed extraction
    leaves the previous structure intact rather than truncating it.
    """
    temp_path = STRUCTURE_PATH + ".new"
    print(f"reading the game cache via {EXTRACTOR}…", flush=True)
    try:
        with open(temp_path, "w") as handle:
            subprocess.run(
                ["node", EXTRACTOR],
                cwd=EXTRACTOR_DIR,
                stdout=handle,
                check=True,
            )
    except FileNotFoundError:
        print("node is not installed; cannot refresh from the cache")
        return 1
    except subprocess.CalledProcessError as exc:
        os.unlink(temp_path)
        print(f"the extractor failed ({exc}). If this is a fresh checkout, run "
              f"`npm install` in {EXTRACTOR_DIR} first.")
        return 1

    os.replace(temp_path, STRUCTURE_PATH)
    print(f"wrote {STRUCTURE_PATH}")
    return 0


def load_structure(path: str = STRUCTURE_PATH) -> dict:
    """The extractor's output, or an exit-worthy error."""
    with open(path) as handle:
        return json.load(handle)


def manifest_payload(extract: dict) -> List[dict]:
    """The manifest section: tabs -> pages -> slot ids, in the game's order.

    Drops the extractor's provenance and struct ids — the manifest is served to
    every client on every load, so it carries only what renders a page.
    """
    tabs = []
    for tab in extract.get("tabs", []):
        pages = []
        for page in tab.get("pages", []):
            items = [int(item) for item in page.get("items", [])]
            if not items:
                continue
            names = list(page.get("names") or [])
            # One name per slot, so the renderer can index them together. A slot
            # the cache could not name is better empty than misaligned.
            names += [""] * (len(items) - len(names))
            pages.append({"name": page["name"], "items": items,
                          "names": names[:len(items)]})
        if pages:
            tabs.append({"name": tab["name"], "pages": pages})
    return tabs


def observed_slot_ids(session) -> Dict[int, int]:
    """Item id -> how many synced accounts report it as a collection log slot.

    This is what the structure is checked against. An id hundreds of accounts
    report is a slot; an id the structure defines that *nobody* reports, while
    its neighbours on the same page have hundreds, is worth a second look.

    Empty on a fresh database, in which case the checks below simply have
    nothing to say. It is a cross-check, never a requirement.
    """
    from sqlalchemy import text

    rows = session.execute(text(
        "SELECT item_id, COUNT(*) FROM player_clog_items GROUP BY item_id"
    )).fetchall()
    return {row[0]: row[1] for row in rows if row[0]}


def reported_slot_total(session) -> int | None:
    """The number of unique slots the game itself says the log has.

    Clients send it with every snapshot, so the most commonly reported value is
    the current game's answer.

    Do not expect it to equal the structure's id count exactly: the game's
    counter and the page enums disagree by a handful (1712 against 1716 as of
    August 2026), and every one of those ids is held by real accounts. It is a
    sanity check on the order of magnitude, not an assertion.
    """
    from sqlalchemy import text

    row = session.execute(text(
        "SELECT clog_slots_total, COUNT(*) AS n FROM player_state "
        "WHERE clog_slots_total > 0 GROUP BY clog_slots_total "
        "ORDER BY n DESC LIMIT 1"
    )).fetchone()
    return int(row[0]) if row else None


def stored_structure(session):
    """The structure currently in the manifest, or None."""
    from db.models import PluginManifestSection

    row = (
        session.query(PluginManifestSection)
        .filter(PluginManifestSection.key == MANIFEST_KEY)
        .first()
    )
    if row is None:
        return None
    try:
        return json.loads(row.payload)
    except (TypeError, ValueError):
        return None


def slot_ids(structure) -> set:
    return {
        item_id
        for tab in structure or []
        for page in tab.get("pages", [])
        for item_id in page.get("items", [])
    }


def slot_names(structure) -> Dict[int, str]:
    """Slot id -> the game's name for it, from the structure."""
    names = {}
    for tab in structure or []:
        for page in tab.get("pages", []):
            page_names = page.get("names") or []
            for index, item_id in enumerate(page.get("items", [])):
                if index < len(page_names) and page_names[index]:
                    names.setdefault(item_id, page_names[index])
    return names


def name_derived_ids(structure, reported: Dict[int, int], item_names: Dict[int, str]):
    """Split ids the structure does not define into noise and candidates.

    A slot reaches us two ways. A full read reports ids straight from the game
    (script 4100), which are always real slots. A single unlock is announced by
    a *chat message* carrying only the item's name, which the plugin resolves
    against RuneLite's item cache — and that returns the earliest id sharing the
    name, which for a duplicated name is the wrong item. "Coal bag" resolves to
    764; the collection log's Coal bag is 25627.

    Those wrong ids are stored (the ingest deliberately keeps what it cannot
    place, so a structure correction repairs accounts retroactively) and then
    sit in the audit looking exactly like the signal it exists to find. They are
    distinguishable, though: a name-derived id is one whose *name* the structure
    already has a slot for. Anything else is a real candidate — most likely a
    game update the extract has not been refreshed for.

    Returns ``(noise, candidates)``; noise entries are
    ``(reported_id, holders, name, [slot ids of that name])``.
    """
    by_name: Dict[str, List[int]] = {}
    for item_id, name in slot_names(structure).items():
        by_name.setdefault(name.strip().lower(), []).append(item_id)

    defined = slot_ids(structure)
    noise, candidates = [], []
    for item_id, holders in sorted(reported.items(), key=lambda kv: -kv[1]):
        if item_id in defined:
            continue
        name = (item_names.get(item_id) or "").strip()
        matches = by_name.get(name.lower(), []) if name else []
        if matches:
            noise.append((item_id, holders, name, sorted(matches)))
        else:
            candidates.append((item_id, holders, name))
    return noise, candidates


def item_names_for(session, item_ids) -> Dict[int, str]:
    """Names from our items table, for ids the structure cannot name."""
    from db.models import ItemList

    if not item_ids:
        return {}
    rows = (
        session.query(ItemList.item_id, ItemList.item_name)
        .filter(ItemList.item_id.in_(list(item_ids)))
        .all()
    )
    return {row[0]: row[1] for row in rows if row[1]}


def audit(session) -> int:
    """Check the stored structure against what accounts actually report.

    Two questions, both of which the collection log page cannot answer for
    itself:

    * which defined slots has *nobody* ever reported (a wrong id, or an item so
      rare that nobody synced holds one), and
    * which reported slots is the structure missing?

    Run it after any game update, and after any change to this script.
    """
    structure = stored_structure(session)
    if structure is None:
        print("No collection_log structure stored — nothing to audit.")
        return 1

    from sqlalchemy import text

    defined = slot_ids(structure)
    observed = observed_slot_ids(session)
    synced = session.execute(
        text("SELECT COUNT(DISTINCT player_id) FROM player_clog_items")
    ).scalar() or 0

    total = reported_slot_total(session)
    print(f"structure: {sum(len(p['items']) for t in structure for p in t['pages'])} slot "
          f"instances, {len(defined)} distinct ids")
    if total:
        print(f"the game's own counter says {total} unique slots; structure defines "
              f"{len(defined)} (a few apart is normal — see reported_slot_total)")
    print(f"{synced} accounts have reported collection log slots")

    never = sorted(defined - set(observed))
    if never:
        names = slot_names(structure)
        print(f"\n{len(never)} defined slots no account has ever reported "
              f"(wrong id, or genuinely nobody owns one):")
        for item_id in never:
            pages = [f"{t['name']}/{p['name']}" for t in structure
                     for p in t["pages"] if item_id in p["items"]]
            print(f"  {item_id:>7}  {names.get(item_id, '?'):<28} {', '.join(pages)}")

    undefined = set(observed) - defined
    names = item_names_for(session, undefined)
    noise, candidates = name_derived_ids(structure, observed, names)

    if candidates:
        print(f"\n{len(candidates)} reported ids the structure does not define and "
              f"cannot explain — refresh the extract if the game has updated:")
        for item_id, holders, name in candidates:
            print(f"  {item_id:>7}  {holders:>4} accounts  {name or '?'}")

    if noise:
        # Not a structure problem: see name_derived_ids. Listed so the count is
        # visible (it should fall to zero once clients stop sending them) but
        # kept out of the signal above.
        print(f"\n{len(noise)} reported ids are the plugin's name-derived unlock path, "
              f"not slots (the structure has the same name under another id):")
        for item_id, holders, name, matches in noise:
            print(f"  {item_id:>7}  {holders:>4} accounts  {name:<28} -> {matches}")

    if not candidates and not noise:
        print("\nEvery reported id is a defined slot.")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-read the game cache into collection_log_structure.json first")
    parser.add_argument("--audit", action="store_true",
                        help="check the stored structure against reported slots and exit")
    parser.add_argument("--force", action="store_true",
                        help="write even if the new structure covers fewer slots than the stored one")
    parser.add_argument("--dump", metavar="PATH",
                        help="also write the manifest payload to a file, for inspection")
    args = parser.parse_args()

    from db.models import Session

    if args.audit:
        session = Session()
        try:
            return audit(session)
        finally:
            session.close()

    if args.refresh:
        failed = refresh_structure()
        if failed:
            return failed

    try:
        extract = load_structure()
    except (OSError, ValueError) as exc:
        print(f"Could not read {STRUCTURE_PATH} ({exc}). Run with --refresh to "
              f"regenerate it from the game cache.")
        return 1

    resolved = manifest_payload(extract)
    tabs = len(resolved)
    pages = sum(len(t["pages"]) for t in resolved)
    slots = sum(len(p["items"]) for t in resolved for p in t["pages"])
    distinct = slot_ids(resolved)
    commit = extract.get("cache_commit")
    print(f"cache {commit[:12] if commit else extract.get('cache_ref')} "
          f"({extract.get('cache_commit_date') or 'date unknown'}): "
          f"{tabs} tabs, {pages} pages, {slots} slots, {len(distinct)} distinct ids")

    session = Session()
    try:
        observed = observed_slot_ids(session)
        previous = slot_ids(stored_structure(session))
        game_total = reported_slot_total(session)
    finally:
        session.close()

    if observed:
        # The cross-check that matters: an id the cache defines but no account
        # holds is either a very rare item or a misread. Naming them here is
        # what makes the difference obvious.
        unheld = sorted(distinct - set(observed))
        held = len(distinct) - len(unheld)
        print(f"{held} of {len(distinct)} defined ids are held by synced accounts")
        if unheld:
            names = slot_names(resolved)
            print(f"  {len(unheld)} held by nobody: "
                  + ", ".join(f"{i} {names.get(i, '?')}" for i in unheld[:10])
                  + (" …" if len(unheld) > 10 else ""))
    if game_total:
        print(f"the game's own counter says {game_total} unique slots")

    added = sorted(distinct - previous)
    lost = sorted(previous - distinct)
    if added:
        names = slot_names(resolved)
        print(f"\n{len(added)} ids this run adds: "
              + ", ".join(f"{i} {names.get(i, '?')}" for i in added[:20])
              + (" …" if len(added) > 20 else ""))
    if lost:
        print(f"\n{len(lost)} ids the stored structure has that this run does not: "
              f"{lost[:20]}{' …' if len(lost) > 20 else ''}")
    if not added and not lost and previous:
        print("\nNo change to the slot ids.")

    if args.dump:
        with open(args.dump, "w") as handle:
            json.dump(resolved, handle)
        print(f"wrote the manifest payload to {args.dump}")

    if not args.apply:
        print("\nDry run — re-run with --apply to write.")
        return 0

    # A run that covers fewer slots than what is already stored is a regression:
    # a truncated extract, or the cache mirror mid-update. Every slot dropped
    # here is a slot that stops rendering for everyone.
    if previous and len(distinct) < len(previous) and not args.force:
        print(f"\nRefusing to write: {len(distinct)} distinct ids is fewer than the "
              f"{len(previous)} already stored. Re-run, or pass --force if the loss "
              f"is intended (a game update removing slots).")
        return 1

    from db.models import PluginManifestSection
    from services.plugin_manifest import CACHE_KEY

    payload = json.dumps(resolved, separators=(",", ":"))
    session = Session()
    try:
        row = (
            session.query(PluginManifestSection)
            .filter(PluginManifestSection.key == MANIFEST_KEY)
            .first()
        )
        description = (
            "Collection log tabs -> pages -> item ids, read from the game cache "
            "by scripts/cache/extract-collection-log.mjs. Defines which slots "
            "exist and how they group."
        )
        if row is None:
            session.add(
                PluginManifestSection(
                    key=MANIFEST_KEY,
                    payload=payload,
                    description=description,
                    source=SOURCE,
                )
            )
        else:
            row.payload = payload
            row.description = description
            row.source = SOURCE
        session.commit()
    finally:
        session.close()

    try:
        from utils.redis import redis_client

        redis_client.delete(CACHE_KEY)
    except Exception as exc:
        print(f"could not invalidate the manifest cache ({exc}); it expires on its own")

    print(f"wrote {MANIFEST_KEY}: {tabs} tabs, {pages} pages, {slots} slots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
