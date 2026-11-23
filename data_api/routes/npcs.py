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

Each NPC also carries ``ehb``: how many kills an efficient hour is worth
(ticket #411). Wise Old Man's published boss rate wins when there is one; for
everything WOM does not price (slayer monsters, newer bosses, raid entry
modes, Doom levels) it is DropTracker's own estimate from
``npc_ehb_rates`` (``scripts/compute_npc_ehb_rates.py``), labelled with how
it was measured. The same order Bingo EHB prices event effort with.

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

#: ``npc_ehb_rates`` is recomputed by hand, rarely; WOM's table weekly.
_EHB_TTL = 3600
_ehb_cache: dict = {"at": 0.0, "derived": None, "wom": None}

#: ``npc_ehb_rates`` methods whose figure is not "kills per hour" — a
#: completion-marker row counts partial attempts — and so is never published.
_UNPUBLISHED_METHODS = frozenset({"partial_gaps"})


def _wom_metric(npc_name: str):
    """The NPC's Wise Old Man boss slug, or None. Memoised: names never change
    meaning, and the full listing asks for every one of them."""
    if npc_name not in _wom_metric_cache:
        try:
            _wom_metric_cache[npc_name] = sect._boss_metric_of(npc_name)
        except Exception:  # a WOM import failure must not cost the listing
            return None
    return _wom_metric_cache[npc_name]


def _wom_ehb_rates() -> dict:
    """``{"main": {slug: kph}, "ironman": {...}}`` from the local copy of WOM's
    rate table (``utils.efficiency``, no network)."""
    try:
        from utils.efficiency import ehb_rates

        return {"main": ehb_rates("main"), "ironman": ehb_rates("ironman")}
    except Exception:
        log.exception("data_api npcs: WOM EHB rates unavailable")
        return {"main": {}, "ironman": {}}


def _derived_ehb_rates(session) -> dict:
    """``{npc_id: (kph, method, sample_size)}`` from ``npc_ehb_rates``, plus
    the same entries under ``("name", npc_match_key(name))``.

    The estimate is computed for one id per monster, but ``npc_list`` holds
    every game id of it (Aberrant spectre is 2..7); the name key lets the
    other ids answer with it. A read failure costs the estimates, never the
    endpoint.
    """
    try:
        rows = session.execute(text(
            "SELECT r.npc_id, r.rate_kph, r.method, r.sample_size, n.npc_name "
            "FROM npc_ehb_rates r LEFT JOIN npc_list n ON n.npc_id = r.npc_id"
        )).fetchall()
    except Exception:
        log.exception("data_api npcs: npc_ehb_rates unreadable")
        try:
            session.rollback()
        except Exception:
            pass
        return {}
    from utils.npc_names import npc_match_key

    out: dict = {}
    by_name: dict = {}
    for nid, kph, method, n, name in rows:
        if not kph or float(kph) <= 0 or method in _UNPUBLISHED_METHODS:
            continue
        entry = (float(kph), method, int(n or 0))
        out[int(nid)] = entry
        key = npc_match_key(name) if name else None
        # Two measured ids under one name: the better-sampled one speaks.
        if key and (key not in by_name or entry[2] > by_name[key][2]):
            by_name[key] = entry
    out.update({("name", k): v for k, v in by_name.items()})
    return out


def ehb_tables(session) -> tuple:
    """``(wom, derived)`` rate tables, cached per process for an hour."""
    now = time.monotonic()
    if _ehb_cache["derived"] is None or now - _ehb_cache["at"] > _EHB_TTL:
        _ehb_cache["wom"] = _wom_ehb_rates()
        _ehb_cache["derived"] = _derived_ehb_rates(session)
        _ehb_cache["at"] = now
    return _ehb_cache["wom"], _ehb_cache["derived"]


def ehb_entry(npc_id: int, name: str, wom_metric, wom: dict, derived: dict):
    """The NPC's kills-per-efficient-hour, or None when nothing prices it.

    WOM's rate for the NPC's metric first (a derived rate never overrides a
    published one), then ours. Boss bodyguards (the GWD sergeants...) are
    None: their kills come with the boss's, whose rate already covers the
    room, so pricing them separately bills one trip several times.
    """
    try:
        from services.event_effort import is_bodyguard

        if name and is_bodyguard(name):
            return None
    except Exception:
        pass
    if wom_metric:
        main = (wom.get("main") or {}).get(wom_metric)
        if main:
            iron = (wom.get("ironman") or {}).get(wom_metric)
            return {
                "kills_per_hour": float(main),
                "kills_per_hour_ironman": float(iron) if iron else None,
                "source": "wom",
                "method": None,
                "sample_size": None,
            }
    found = derived.get(int(npc_id))
    if not found and name:
        from utils.npc_names import npc_match_key

        found = derived.get(("name", npc_match_key(name)))
    if found:
        kph, method, sample = found
        return {
            "kills_per_hour": round(kph, 1),
            "kills_per_hour_ironman": None,
            "source": "droptracker",
            "method": method,
            "sample_size": sample,
        }
    return None


def npc_row(npc_id: int, name: str, ehb_rates: tuple | None = None) -> dict:
    """``ehb_rates`` is :func:`ehb_tables`' pair; omitted, ``ehb`` is null."""
    metric = _wom_metric(name) if name else None
    wom, derived = ehb_rates or ({}, {})
    return {
        "npc_id": int(npc_id),
        "name": name or None,
        "synthetic": int(npc_id) >= SYNTHETIC_NPC_ID_FLOOR,
        "wom_metric": metric,
        "ehb": ehb_entry(npc_id, name, metric, wom, derived),
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
        rates = ehb_tables(session)
        npcs = [npc_row(nid, nm, rates) for nid, nm in rows]
        return {"count": len(npcs), "npcs": npcs}

    filtered = name is not None or query is not None
    return await serve_fixed(
        "npcs.search" if filtered else "npcs.list",
        NPC_SEARCH_COST if filtered else NPC_LIST_COST,
        build)


@npcs_bp.route("/npcs/<int:npc_id>", methods=["GET"])
async def get_npc(npc_id: int):
    """One NPC: its name, whether the id is the game's, its WOM slug, its EHB
    rate, and the wiki drop table."""

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
        body = npc_row(npc_id, name, ehb_tables(session))
        body["icon_url"] = f"{IMG_BASE}/npcdb/{npc_id}.png"
        body["wiki_url"] = f"{WIKI_BASE}/{quote(name.replace(' ', '_'))}" if name else None
        body["drop_table"] = _drop_table(session, npc_id, name, rows)
        return body

    return await serve_fixed(
        "npcs.detail", NPC_DETAIL_COST, build,
        not_found_detail=f"No NPC with id {npc_id} is known to DropTracker.")
