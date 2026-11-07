"""``/v2/npcs`` — DropTracker's NPC catalogue as reference data.

Every ``npc_id`` the API hands out (``personal_bests``, ``drops``,
``loot_npcs``) is a row of ``npc_list``. Integrations kept their own
id -> name tables and re-edited them on every new boss (suggestion #232);
these endpoints are the source those tables should be built from.

Most ids are the game's own NPC ids. Content with no single NPC to name —
raids, minigames, reward chests, activities — gets a DropTracker-assigned id
of 900000 or above, flagged ``synthetic`` so a consumer does not go looking
for it in the game cache or on the wiki's NPC id lists.

The drop table is the wiki's (``xenforo.dt_npc_loot``, resynced weekly by
``scripts/sync_wiki_drops.py``), with the same boss-family fallback the
droptracker.io NPC page uses: the wiki import put each family's table on one
member (the CoX table on the base raid, ToB's on Hard Mode), so a mode or
spelling variant borrows its nearest relative's and says which one.

Game data, not player data: any valid key may read it, at a flat cost.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote

from quart import Blueprint, request
from sqlalchemy import bindparam, text

from data_api import sections as sect
from data_api.serving import _json, serve_fixed

log = logging.getLogger(__name__)

npcs_bp = Blueprint("npcs", __name__)

#: Ids at or above this are DropTracker's own, not the game's.
SYNTHETIC_NPC_ID_FLOOR = 900_000

#: ~3,700 rows from the process cache; the price is serialisation and bandwidth.
NPC_LIST_COST = 10
#: A filtered list (``name`` / ``q``) is a handful of rows.
NPC_SEARCH_COST = 2
#: One npc_list row plus one indexed read of the wiki table.
NPC_DETAIL_COST = 5

#: The wiki table changes once a week; an hour per NPC per process is plenty.
_DROP_TABLE_TTL = 3600
_drop_table_cache: dict = {}

#: MariaDB "SELECT command denied": the data API's read-only account has no
#: grant on the xenforo schema (see ``_drop_table``).
_ACCESS_DENIED = 1142

IMG_BASE = "https://www.droptracker.io/img"
WIKI_BASE = "https://oldschool.runescape.wiki/w"

_wom_metric_cache: dict = {}


def _wom_metric(npc_name: str):
    """The NPC's Wise Old Man boss slug, or None. Memoised: names never change
    meaning, and the full listing asks for every one of them."""
    if npc_name not in _wom_metric_cache:
        try:
            _wom_metric_cache[npc_name] = sect._boss_metric_of(npc_name)
        except Exception:  # a WOM import failure must not cost the listing
            return None
    return _wom_metric_cache[npc_name]


def npc_row(npc_id: int, name: str) -> dict:
    return {
        "npc_id": int(npc_id),
        "name": name or None,
        "synthetic": int(npc_id) >= SYNTHETIC_NPC_ID_FLOOR,
        "wom_metric": _wom_metric(name) if name else None,
    }


def filter_npcs(rows: list, name: str | None, query: str | None, resolve) -> list:
    """``rows`` (``(npc_id, name)``) narrowed by ``name`` and/or ``q``.

    ``name`` is identity, through ``resolve`` (the drop feed's resolver: a
    boss name in any spelling, or a WOM slug). ``q`` is a case-insensitive
    substring of the name, for "every Doom of Mokhaiotl level". Both given
    means both apply.
    """
    if name is not None:
        wanted = set(resolve(name))
        rows = [r for r in rows if r[0] in wanted]
    if query is not None:
        needle = query.lower()
        rows = [r for r in rows if needle in (r[1] or "").lower()]
    return sorted(rows, key=lambda r: r[0])


def drop_table_items(rows) -> list:
    """Wiki rows ``(item_id, item_name, quantity, noted, rarity, rolls)`` ->
    API entries, in stored order.

    ``dt_npc_loot`` repeats identical lines (up to 60 copies on CoX); those
    collapse. Distinct lines for one item (two rarity tiers, two quantities)
    are kept — they are different drops. Rarity is the per-roll chance; the
    wiki's unparseable rates are stored as 0 and come out as null.
    """
    seen = set()
    items = []
    for item_id, item_name, quantity, noted, rarity, rolls in rows:
        item_id = int(item_id)
        rarity = float(rarity or 0)
        rolls = int(rolls or 1)
        key = (item_id, str(quantity), bool(noted), rarity, rolls)
        if key in seen:
            continue
        seen.add(key)
        items.append({
            "item_id": item_id,
            "item_name": item_name or None,
            "quantity": None if quantity is None else str(quantity),
            "noted": bool(noted),
            "rarity": rarity if rarity > 0 else None,
            "rolls": rolls,
        })
    return items


def _wiki_rows(session, npc_ids: list) -> dict:
    """``{npc_id: [row, ...]}`` for every id that has wiki lines."""
    if not npc_ids:
        return {}
    result = session.execute(
        text(
            "SELECT l.npc_id, l.item_id, i.item_name, l.quantity, l.noted, l.rarity, l.rolls "
            "FROM xenforo.dt_npc_loot l "
            "LEFT JOIN items i ON i.item_id = l.item_id "
            "WHERE l.npc_id IN :ids ORDER BY l.rarity DESC, l.item_id ASC"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": list(npc_ids)},
    )
    out: dict = {}
    for npc_id, *row in result:
        out.setdefault(int(npc_id), []).append(tuple(row))
    return out


def family_candidates(npc_id: int, npc_name: str, rows: list) -> list:
    """Other ``npc_list`` ids in this boss's family, as priority tiers of
    ascending ids — the Python twin of the site's ``_family_table_rows``,
    run over the cached rows instead of a REGEXP scan of the table."""
    from utils.npc_names import npc_family_tiers, npc_slug

    slugs = [(nid, npc_slug(name)) for nid, name in rows if nid != npc_id]
    tiers = []
    for tier in npc_family_tiers(npc_name):
        wanted = set(tier)
        ids = sorted(nid for nid, slug in slugs if slug in wanted)
        if ids:
            tiers.append(ids)
    return tiers


def _drop_table(session, npc_id: int, npc_name: str, rows: list):
    """The NPC's drop table, or None when it cannot be read.

    Needs ``GRANT SELECT ON xenforo.dt_npc_loot TO dataapi_ro`` — this
    service's account is scoped to the ``data`` schema. Without it the rest
    of the NPC still answers and ``drop_table`` is null, rather than a 500.
    """
    cached = _drop_table_cache.get(npc_id)
    if cached is not None and time.monotonic() - cached[0] < _DROP_TABLE_TTL:
        return cached[1]

    try:
        source, source_npc_id = None, None
        found = _wiki_rows(session, [npc_id]).get(npc_id)
        if found:
            source, source_npc_id = "wiki", npc_id
        else:
            for tier in family_candidates(npc_id, npc_name, rows):
                by_id = _wiki_rows(session, tier)
                donor = next((nid for nid in tier if by_id.get(nid)), None)
                if donor is not None:
                    found, source, source_npc_id = by_id[donor], "wiki_family", donor
                    break
    except Exception as exc:
        cause = getattr(exc, "orig", exc)
        if getattr(cause, "args", (None,))[0] != _ACCESS_DENIED:
            raise
        log.error("data_api npcs: no SELECT grant on xenforo.dt_npc_loot; "
                  "drop tables are unavailable")
        session.rollback()
        return None

    table = {
        "source": source,
        "source_npc_id": source_npc_id,
        "items": drop_table_items(found or []),
    }
    _drop_table_cache[npc_id] = (time.monotonic(), table)
    return table


def _optional_text(name: str):
    raw = request.args.get(name)
    if raw is None:
        return None, None
    if not raw.strip():
        return None, _json({
            "error": "malformed_parameter",
            "detail": f"'{name}' must not be empty.",
        }, 400)
    return raw.strip(), None


@npcs_bp.route("/npcs", methods=["GET"])
async def list_npcs():
    """Every NPC id DropTracker knows, with its name.

    ``?name=`` narrows to one boss (any spelling, or a Wise Old Man slug);
    ``?q=`` to names containing the text. Unfiltered, it is the whole
    catalogue — fetch it once and cache it, new ids appear only when new
    content does.
    """
    name, error = _optional_text("name")
    if error is not None:
        return error
    query, error = _optional_text("q")
    if error is not None:
        return error

    def build(session):
        rows = sect._npc_rows(session)
        if name is not None or query is not None:
            rows = filter_npcs(rows, name, query,
                               lambda raw: sect.resolve_npc_ids(session, raw))
        else:
            rows = sorted(rows, key=lambda r: r[0])
        npcs = [npc_row(nid, nm) for nid, nm in rows]
        return {"count": len(npcs), "npcs": npcs}

    filtered = name is not None or query is not None
    return await serve_fixed(
        "npcs.search" if filtered else "npcs.list",
        NPC_SEARCH_COST if filtered else NPC_LIST_COST,
        build)


@npcs_bp.route("/npcs/<int:npc_id>", methods=["GET"])
async def get_npc(npc_id: int):
    """One NPC: its name, whether the id is the game's, its WOM slug, and the
    wiki drop table."""

    def build(session):
        rows = sect._npc_rows(session)
        name = next((nm for nid, nm in rows if nid == npc_id), None)
        if name is None:
            # Not in the 5-minute cache: a brand-new row, or no such NPC.
            row = session.execute(
                text("SELECT npc_name FROM npc_list WHERE npc_id = :nid"),
                {"nid": npc_id},
            ).first()
            if row is None:
                return None
            name = row[0] or ""
        body = npc_row(npc_id, name)
        body["icon_url"] = f"{IMG_BASE}/npcdb/{npc_id}.png"
        body["wiki_url"] = f"{WIKI_BASE}/{quote(name.replace(' ', '_'))}" if name else None
        body["drop_table"] = _drop_table(session, npc_id, name, rows)
        return body

    return await serve_fixed(
        "npcs.detail", NPC_DETAIL_COST, build,
        not_found_detail=f"No NPC with id {npc_id} is known to DropTracker.")
