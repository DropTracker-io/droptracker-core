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


#: Group config: which collection log slots a group posts (suggestion #238).
#: ``all`` (default, or unset) = every new slot; ``completed`` = only the slot
#: that completes a collection log page.
NOTIFY_MODE_KEY = "clog_notify_mode"
NOTIFY_MODES = ("all", "completed")


def notify_mode(value) -> str:
    """The stored ``clog_notify_mode`` as a known mode, ``all`` when unset or unknown."""
    mode = str(value or "").strip().lower()
    return mode if mode in NOTIFY_MODES else "all"


@lru_cache(maxsize=1)
def _layout() -> tuple[list, dict]:
    """``([(page title, item ids, lower-case names)], {id: its other id})``.

    The other-id map pairs each stored slot id with the id it is drawn as (see
    ``_index``), both ways, so a slot counts whichever one a source recorded.
    """
    try:
        with open(STRUCTURE_PATH, encoding="utf-8") as f:
            structure = json.load(f)
    except (OSError, ValueError):
        return [], {}
    other: dict[int, int] = {}
    for k, v in (structure.get("replacements") or {}).items():
        other[int(k)], other[int(v)] = int(v), int(k)
    pages = []
    for tab in structure.get("tabs") or []:
        for page in tab.get("pages") or []:
            title, items = page.get("name"), [int(i) for i in page.get("items") or []]
            if title and items:
                names = [str(n).strip().lower() for n in page.get("names") or []]
                pages.append((title, items, names))
    return pages, other


def completed_page(session, player_id, item_id=None, item_name=None, source=None) -> Optional[str]:
    """The collection log page this new slot completes, or ``None``.

    A slot counts as filled when the player's synced log holds it
    (``player_clog_items``, written when they open the log with the plugin) or
    we recorded its unlock (``collection``), plus the slot being announced now,
    which the synced copy may not have caught up with. A player who never
    synced only completes a page whose every slot we saw them unlock, so the
    answer leans to "not complete": this feeds a noise filter, and a page we
    cannot vouch for is better left unannounced than announced wrongly.

    When the item sits on several pages and more than one is now complete, the
    page matching ``source`` wins, as in :func:`collection_for_item`.
    """
    pages, other = _layout()
    if not pages:
        return None
    try:
        new_id = int(item_id) if item_id not in (None, "") else None
    except (TypeError, ValueError):
        new_id = None
    new_name = str(item_name or "").strip().lower()

    def is_new(index, slot_id, names):
        if new_id is not None and new_id in (slot_id, other.get(slot_id)):
            return True
        return bool(new_name) and index < len(names) and names[index] == new_name

    candidates = [
        (title, items, names) for title, items, names in pages
        if any(is_new(i, slot, names) for i, slot in enumerate(items))
    ]
    if not candidates:
        return None

    from sqlalchemy import text

    rows = session.execute(
        text(
            "SELECT item_id FROM player_clog_items WHERE player_id = :pid AND quantity > 0 "
            "UNION SELECT item_id FROM collection WHERE player_id = :pid"
        ),
        {"pid": int(player_id)},
    ).fetchall()
    held = {int(r[0]) for r in rows if r[0] is not None}

    complete = [
        title for title, items, names in candidates
        if all(
            slot in held or other.get(slot) in held or is_new(i, slot, names)
            for i, slot in enumerate(items)
        )
    ]
    if len(complete) > 1 and source:
        from utils.npc_names import npc_match_key

        wanted = npc_match_key(str(source))
        for title in complete:
            if wanted and wanted in {npc_match_key(part) for part in [title, *title.split(" and ")]}:
                return title
    return complete[0] if complete else None
