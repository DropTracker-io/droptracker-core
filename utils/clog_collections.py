"""Which collection log page an item belongs to, for the ``{collection_name}`` token.

Read from ``scripts/collection_log_structure.json``, the page/slot layout
extracted from the game cache (see ``scripts/sync_collection_log.py``). No
sender ever filled ``collection_name``, so the token rendered as ``None`` on
every clog post (ticket #476).

About 150 items sit on more than one page (Dragon pickaxe, the clue uniques,
...). When the submission names its source, the page matching that source wins;
otherwise the first page in log order does.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

STRUCTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "collection_log_structure.json",
)


@lru_cache(maxsize=1)
def _index() -> tuple[dict, dict]:
    """``(pages by item id, pages by lower-case item name)``, in log order."""
    by_id: dict[int, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    try:
        with open(STRUCTURE_PATH, encoding="utf-8") as f:
            structure = json.load(f)
    except (OSError, ValueError):
        return by_id, by_name
    # The cache stores a dozen slots under one id and draws them as another
    # (enum 3721). Pages list the drawn id, which clients report; index the
    # stored id as well so either finds the page.
    drawn = {int(k): int(v) for k, v in (structure.get("replacements") or {}).items()}
    stored = {v: k for k, v in drawn.items()}
    for tab in structure.get("tabs") or []:
        for page in tab.get("pages") or []:
            title = page.get("name")
            if not title:
                continue
            for item_id in page.get("items") or []:
                item_id = int(item_id)
                for key in {item_id, drawn.get(item_id, item_id), stored.get(item_id, item_id)}:
                    pages = by_id.setdefault(key, [])
                    if title not in pages:
                        pages.append(title)
            for name in page.get("names") or []:
                pages = by_name.setdefault(str(name).strip().lower(), [])
                if title not in pages:
                    pages.append(title)
    return by_id, by_name


def collection_for_item(item_id=None, item_name=None, source=None) -> Optional[str]:
    """The collection log page for an item, or ``None`` when it is not a slot."""
    by_id, by_name = _index()
    pages = []
    try:
        if item_id not in (None, ""):
            pages = by_id.get(int(item_id), [])
    except (TypeError, ValueError):
        pages = []
    if not pages and item_name:
        pages = by_name.get(str(item_name).strip().lower(), [])
    if not pages:
        return None
    if len(pages) > 1 and source:
        from utils.npc_names import npc_match_key

        wanted = npc_match_key(str(source))
        for page in pages:
            # "Venenatis and Spindel" is the page for either boss.
            if wanted and wanted in {npc_match_key(part) for part in [page, *page.split(" and ")]}:
                return page
    return pages[0]
