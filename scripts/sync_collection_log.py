"""Scrape the collection log's structure from the OSRS Wiki into the manifest.

Why this exists: the collection log page could only render a flat grid of every
slot we happened to know about, because nothing told us which slots belong to
which page ("Abyssal Sire", "Barrows Chests", ...) or which tab those pages live
under. That structure is what makes the in-game interface legible, and without
it the page is a wall of icons.

**Source.** The OSRS Wiki's "Collection log" article, which lays the hierarchy
out exactly as the game does: ``== Bosses ==`` tabs containing ``=== Abyssal
Sire ===`` pages, each with a table of items. The wiki is CC BY-NC-SA and is the
community's canonical published copy of this data.

Deliberately *not* copied from RuneProfile's repository. They hold the same
structure in ``packages/runescape/src/collection-log.ts``, but that repository
carries no licence at all, which makes it all-rights-reserved. They derive it
from the game cache; we derive it from the wiki. Same facts, a source we can
actually rely on.

Item **names** are what the wiki publishes, so they are resolved to ids against
our own ``items`` table. Anything unresolved is reported rather than silently
dropped — a missing item is a slot a player can never be shown as having.

    ./venv/bin/python -m scripts.sync_collection_log            # dry run
    ./venv/bin/python -m scripts.sync_collection_log --apply    # write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Dict, List, Tuple

import requests

API_URL = "https://oldschool.runescape.wiki/api.php"
PAGE = "Collection log"
USER_AGENT = (
    "DropTracker/1.0 (https://www.droptracker.io; collection log structure sync)"
)

MANIFEST_KEY = "collection_log"
SOURCE = "scripts/sync_collection_log.py"

# Headings that are page furniture rather than collection log tabs.
NON_TAB_HEADINGS = {"Combat stats", "Ranks", "Changes", "Gallery", "References",
                    "Trivia", "See also", "Update history"}

# ``{{plink|Item name}}`` and its variants (``plinkp``, ``plinkt``), optionally
# with extra template parameters we do not care about.
PLINK_RE = re.compile(r"\{\{plink[a-z]*\|([^|}]+)", re.IGNORECASE)

# A section's items live in the wikitable that follows its heading.
HEADING_RE = re.compile(r"^(={2,3})\s*(.+?)\s*\1\s*$", re.MULTILINE)


def fetch_wikitext() -> str:
    response = requests.get(
        API_URL,
        params={"format": "json", "action": "parse", "page": PAGE, "prop": "wikitext"},
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"wiki error: {body['error']}")
    return body["parse"]["wikitext"]["*"]


def parse_structure(wikitext: str) -> List[Tuple[str, List[Tuple[str, List[str]]]]]:
    """[(tab, [(page, [item name, ...]), ...]), ...] in the wiki's own order.

    The wiki's ordering matches the game's, so it is preserved rather than
    sorted: a collection log in alphabetical order would not look like the
    collection log.
    """
    matches = list(HEADING_RE.finditer(wikitext))
    tabs: List[Tuple[str, List[Tuple[str, List[str]]]]] = []
    current_tab: str | None = None

    for i, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        # Strip any wiki markup left in a heading (links, formatting).
        title = re.sub(r"\[\[([^\]|]*\|)?([^\]]+)\]\]", r"\2", title).strip("' ")

        if level == 2:
            current_tab = None if title in NON_TAB_HEADINGS else title
            if current_tab:
                tabs.append((current_tab, []))
            continue

        if level != 3 or current_tab is None or not tabs:
            continue

        end = matches[i + 1].start() if i + 1 < len(matches) else len(wikitext)
        section = wikitext[match.end():end]

        names: List[str] = []
        seen = set()
        for raw in PLINK_RE.findall(section):
            name = raw.strip()
            # Some entries carry a display suffix after a slash or comment.
            name = re.sub(r"<!--.*?-->", "", name).strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            names.append(name)

        if names:
            tabs[-1][1].append((title, names))

    return [(tab, pages) for tab, pages in tabs if pages]


# ``|id = 4071`` in an item's infobox. Some pages list several ids (variants);
# the first is the one the page is about.
INFOBOX_ID_RE = re.compile(r"^\|\s*id\s*=\s*(\d+)", re.MULTILINE)


def lookup_item_ids_on_wiki(names: List[str]) -> Dict[str, int]:
    """Item ids for names our own table cannot resolve.

    The wiki disambiguates items that share a display name — "Decorative helm
    (red)", "Graceful hood (Agility Arena)" — while our items table stores one
    row per name, so those lookups miss. Rather than guess (matching the base
    name would map three Castle Wars variants onto one id), ask the wiki for the
    id on each disambiguated page.

    Names it cannot answer for are simply absent; the caller reports them.
    """
    found: Dict[str, int] = {}
    batch_size = 40  # the API caps titles per request
    for start in range(0, len(names), batch_size):
        batch = names[start:start + batch_size]
        try:
            response = requests.get(
                API_URL,
                params={
                    "format": "json",
                    "action": "query",
                    "titles": "|".join(batch),
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                },
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            response.raise_for_status()
            pages = response.json().get("query", {}).get("pages", {})
        except Exception as exc:
            print(f"  wiki lookup failed for a batch ({exc}); continuing")
            continue

        for page in pages.values():
            title = page.get("title")
            revisions = page.get("revisions")
            if not title or not revisions:
                continue
            try:
                text = revisions[0]["slots"]["main"]["*"]
            except (KeyError, IndexError, TypeError):
                continue
            match = INFOBOX_ID_RE.search(text)
            if match:
                found[title.strip().lower()] = int(match.group(1))
    return found


def resolve_items(structure) -> Tuple[List[dict], List[str]]:
    """Turn item names into ids using our own item table.

    Matching is case-insensitive on the exact name. Deliberately no fuzzy
    matching: a wrong id would silently attribute the wrong item to a page,
    which is worse than a reported miss.
    """
    from db.models import ItemList, Session

    session = Session()
    try:
        by_name: Dict[str, int] = {}
        for item_id, name in session.query(ItemList.item_id, ItemList.item_name).all():
            if name:
                by_name.setdefault(name.strip().lower(), item_id)
    finally:
        session.close()

    # First pass: find every name our table cannot answer, then ask the wiki
    # for all of them at once rather than one request per miss.
    unresolved_names = []
    for _tab, pages in structure:
        for _page, names in pages:
            for name in names:
                if name.lower() not in by_name and name not in unresolved_names:
                    unresolved_names.append(name)

    from_wiki: Dict[str, int] = {}
    if unresolved_names:
        print(f"asking the wiki for {len(unresolved_names)} disambiguated item ids…",
              flush=True)
        from_wiki = lookup_item_ids_on_wiki(unresolved_names)
        print(f"  the wiki resolved {len(from_wiki)} of them")

    resolved = []
    missing: List[str] = []
    for tab, pages in structure:
        tab_entry = {"name": tab, "pages": []}
        for page, names in pages:
            ids = []
            for name in names:
                item_id = by_name.get(name.lower()) or from_wiki.get(name.lower())
                if item_id is None:
                    missing.append(f"{tab}/{page}: {name}")
                    continue
                ids.append(item_id)
            if ids:
                tab_entry["pages"].append({"name": page, "items": ids})
        if tab_entry["pages"]:
            resolved.append(tab_entry)
    return resolved, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--show-missing", action="store_true",
                        help="list every item name that could not be resolved")
    args = parser.parse_args()

    print("fetching the wiki's collection log article…", flush=True)
    structure = parse_structure(fetch_wikitext())
    tabs = len(structure)
    pages = sum(len(p) for _t, p in structure)
    names = sum(len(i) for _t, p in structure for _n, i in p)
    print(f"parsed {tabs} tabs, {pages} pages, {names} item names")

    resolved, missing = resolve_items(structure)
    slots = sum(len(p["items"]) for t in resolved for p in t["pages"])
    print(f"resolved {slots} slots; {len(missing)} names unresolved")
    if missing and args.show_missing:
        for line in missing:
            print(f"  ? {line}")

    if not args.apply:
        print("\nDry run — re-run with --apply to write.")
        return 0

    from db.models import PluginManifestSection, Session
    from services.plugin_manifest import CACHE_KEY

    payload = json.dumps(resolved, separators=(",", ":"))
    session = Session()
    try:
        row = (
            session.query(PluginManifestSection)
            .filter(PluginManifestSection.key == MANIFEST_KEY)
            .first()
        )
        if row is None:
            session.add(
                PluginManifestSection(
                    key=MANIFEST_KEY,
                    payload=payload,
                    description=(
                        "Collection log tabs -> pages -> item ids, scraped from the "
                        "OSRS Wiki. Defines which slots exist and how they group."
                    ),
                    source=SOURCE,
                )
            )
        else:
            row.payload = payload
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
