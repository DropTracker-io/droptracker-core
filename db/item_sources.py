"""Which NPCs drop a given item — shared between the web API and the workers.

The queries live here rather than in ``web_api/routes/items.py`` because the
events worker needs the same answer (see ``services/event_effort.py``: an item
task's *effort* NPCs are the item's drop sources) and a worker must never
import a route module.

Two sources are unioned, and both are needed:

* ``xenforo.dt_npc_loot`` — the wiki drop table, carrying ``rarity`` / ``rolls``
  (``rarity * rolls`` = per-kill probability). Authoritative where it has rows,
  but it lags new content badly: Yama and Doom of Mokhaiotl have none at all.
* observed ``drops`` rows — NPCs we have actually seen drop the item. Covers
  the wiki's gaps (Yama has ~196k tracked drops), at the cost of a bounded
  random-read scan, and is threshold-gated so a single misattributed drop
  cannot invent a source.

Module imports are stdlib + SQLAlchemy only and DB models are imported lazily
inside functions, so this module loads under the unit-test suite's stubbed
``db`` package (``tests/conftest.py`` registers it by path).
"""
from __future__ import annotations

from sqlalchemy import bindparam, text

#: Most NPC entries any one item resolves to. Must exceed the wiki's most
#: widely-sourced item ("Coins": 601 distinct sources) — the query keeps the
#: RAREST rows, so a cap below the real source count silently hides the
#: common-table ones: Uncut diamond has ~453 sources and a 100-cap cut off
#: everything below 1/8,192, including every GWD boss's 1/2,501 gem-table
#: line, so the event task-form source picker couldn't offer Kree'arra.
SOURCES_LIMIT = 1000

# Tracked-drop fallback: the wiki table misses whole activity sources (e.g.
# Wintertodt's reward cart has zero wiki rows), so NPCs we've actually OBSERVED
# dropping the item are unioned in. The scan samples at most this many drop
# rows so ultra-common items stay bounded, and a source only qualifies past
# both thresholds — filtering one-off misattributions (a "Bird nest" that once
# reported a dragon axe).
TRACKED_SOURCE_SCAN_ROWS = 50_000
TRACKED_SOURCE_MIN_DROPS = 5
TRACKED_SOURCE_MIN_PLAYERS = 3

#: Skip the tracked-drop fallback scan once the wiki already knows this many
#: sources for the item. The scan exists to fill wiki GAPS (new content with
#: no wiki rows); an item with 100+ wiki sources has no gap worth the ~10s
#: 50k-row random-read scan. Decoupled from SOURCES_LIMIT: the cap had to grow
#: past the largest real source list (601), which would otherwise have turned
#: the scan back ON for exactly the ubiquitous items where it is most costly.
OBSERVED_FALLBACK_MAX_WIKI_SOURCES = 100


def variant_item_ids(s, item_name: str | None, fallback_id: int) -> list[int]:
    """Every ``items`` id sharing ``item_name`` (noted variants included).

    One OSRS item name spans several ids — stack-size variants, the noted
    copy, placeholder/beta rows — and a submitted drop is recorded under
    whichever id the client sent, which is often not the lowest. Anything that
    reads a per-item-id table for a *name* (drop sources, receivable probes)
    must cover the whole set or it reads a variant with no rows at all.
    ``fallback_id`` covers a name that resolves to nothing (or is NULL).
    """
    from db.models import ItemList

    name = (item_name or "").strip()
    if not name:
        return [int(fallback_id)]
    ids = [
        int(i)
        for (i,) in s.query(ItemList.item_id).filter(ItemList.item_name == name).all()
    ]
    return ids or [int(fallback_id)]


def item_ids_for_name(s, item_name: str | None) -> list[int]:
    """Every ``items`` id for ``item_name``; ``[]`` when the name is unknown.

    :func:`variant_item_ids` without a fallback id — callers that start from a
    *name* (event task configs do) have no id to fall back to, and an unknown
    item must resolve to no sources rather than to item 0's.
    """
    from db.models import ItemList

    name = (item_name or "").strip()
    if not name:
        return []
    return [
        int(i)
        for (i,) in s.query(ItemList.item_id).filter(ItemList.item_name == name).all()
    ]


def observed_source_rows(s, item_ids) -> list[tuple[int, str, int]]:
    """``(npc_id, npc_name, drop_count)`` for NPCs we've actually observed
    dropping this item, most-seen first — the wiki-gap fallback feeding
    :func:`source_npc_rows`. Samples at most ``TRACKED_SOURCE_SCAN_ROWS`` drop
    rows (bounded on ultra-common items) and applies the min-drops /
    min-players thresholds so one-off misattributed drops don't invent a
    source.

    Takes every id the item name maps to — see :func:`source_npc_rows`."""
    rows = s.execute(
        text("SELECT npc_id, player_id FROM drops WHERE item_id IN :ids LIMIT :lim")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": list(item_ids), "lim": TRACKED_SOURCE_SCAN_ROWS},
    ).fetchall()
    drops_by_npc: dict[int, int] = {}
    players_by_npc: dict[int, set] = {}
    for npc_id, player_id in rows:
        if npc_id is None:
            continue
        nid = int(npc_id)
        drops_by_npc[nid] = drops_by_npc.get(nid, 0) + 1
        players_by_npc.setdefault(nid, set()).add(player_id)
    qualifying = [
        nid
        for nid, count in drops_by_npc.items()
        if count >= TRACKED_SOURCE_MIN_DROPS
        and len(players_by_npc[nid]) >= TRACKED_SOURCE_MIN_PLAYERS
    ]
    if not qualifying:
        return []
    names = dict(
        s.execute(
            text("SELECT npc_id, npc_name FROM npc_list WHERE npc_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": qualifying},
        ).fetchall()
    )
    out = [(nid, names[nid], drops_by_npc[nid]) for nid in qualifying if nid in names]
    out.sort(key=lambda r: -r[2])
    return out


def source_npc_rows(s, item_ids, *, limit: int = SOURCES_LIMIT) -> tuple[int, list[dict]]:
    """``(wiki_total, [{npc_id, name, quantity, rarity, rolls, tracked,
    observed}])`` — the wiki drop table (rarest first) unioned with observed
    sources it misses.

    ``wiki_total`` counts distinct wiki source names *before* the cap, so it
    can exceed ``len(rows)``; ``observed`` marks the wiki-gap fallback entries,
    which are not in that count. Callers that report a total add the observed
    ones back (see ``web_api/routes/items.py::_sources``).

    Takes EVERY ``items`` id the item's name maps to, not one. Both source
    tables are keyed by item id, but an OSRS item name spans several ids
    (stack sizes, noted, placeholder variants) and a drop is recorded under
    whichever id the client sent — usually not the lowest. Asking about a
    single id therefore answers about an id with no rows: "Vial of blood"
    resolved to 22405 (wiki: Hard Mode only, zero drops) while every real
    receipt sits on 22446, so the task-form picker offered one ToB variant
    out of three. Callers pass the whole variant set.

    Presentation (icons, alias collapsing) is deliberately NOT done here — see
    ``web_api/routes/items.py::_sources``.
    """
    ids = sorted({int(i) for i in item_ids})
    if not ids:
        return 0, []

    total = s.execute(
        text(
            "SELECT COUNT(DISTINCT n.npc_name) FROM xenforo.dt_npc_loot l "
            "JOIN npc_list n ON n.npc_id = l.npc_id WHERE l.item_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).fetchone()
    # One row per npc NAME, chosen in SQL (ROW_NUMBER) rather than by
    # truncate-then-dedupe in Python: dt_npc_loot repeats a drop-table line
    # per wiki revision (125k rows for 62k distinct item/npc pairs) and
    # replicates it across multi-form boss ids, so a bare `LIMIT` spent its
    # whole budget on duplicates — "Uncut ruby" has 261 distinct sources but
    # surfaced only 34. `tracked DESC` still prefers the id variant drops
    # actually land on, so the entry links to the NPC page that has data.
    rows = s.execute(
        text(
            "WITH src AS ("
            "  SELECT l.npc_id, n.npc_name, l.quantity, l.rarity, l.rolls, "
            "         EXISTS(SELECT 1 FROM player_npc_hourly_totals t "
            "                WHERE t.npc_id = l.npc_id) AS tracked "
            "  FROM xenforo.dt_npc_loot l "
            "  JOIN npc_list n ON n.npc_id = l.npc_id "
            "  WHERE l.item_id IN :ids"
            ") "
            "SELECT npc_id, npc_name, quantity, rarity, rolls, tracked FROM ("
            "  SELECT src.*, ROW_NUMBER() OVER ("
            "           PARTITION BY npc_name "
            "           ORDER BY rarity ASC, tracked DESC, npc_id ASC) AS rn "
            "  FROM src"
            ") x WHERE x.rn = 1 "
            "ORDER BY rarity ASC, tracked DESC, npc_id ASC LIMIT :lim"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": ids, "lim": limit},
    ).fetchall()
    # The observed scan is a wiki-GAP fallback whose extras are discarded
    # once the list is full, and it costs a random row read per sampled
    # drop (~10s for an item as common as Uncut ruby, which has no gap to
    # fill — the wiki already lists hundreds of sources for it). Skip it
    # once the wiki rows show no meaningful gap or already fill a small
    # caller-imposed cap: identical output, none of the cost.
    observed = (
        observed_source_rows(s, ids)
        if len(rows) < min(OBSERVED_FALLBACK_MAX_WIKI_SOURCES, limit)
        else []
    )

    npcs: list[dict] = []
    seen_names = set()
    for npc_id, npc_name, quantity, rarity, rolls, tracked in rows:
        seen_names.add(npc_name)
        npcs.append(
            {
                "npc_id": int(npc_id),
                "name": npc_name,
                "quantity": str(quantity),
                "rarity": float(rarity),
                "rolls": int(rolls or 1),
                # Whether we have tracked-drop history for this NPC — lets the
                # event task-source picker warn on sources we've never observed.
                "tracked": bool(tracked),
                "observed": False,
            }
        )
    # Wiki-gap fallback: sources we've observed dropping the item but the wiki
    # table doesn't know. Rarity 0 renders as "—" (no wiki rate to show).
    for npc_id, npc_name, _count in observed:
        if npc_name in seen_names or len(npcs) >= limit:
            continue
        seen_names.add(npc_name)
        npcs.append(
            {
                "npc_id": int(npc_id),
                "name": npc_name,
                "quantity": "1",
                "rarity": 0.0,
                "rolls": 1,
                "tracked": True,
                "observed": True,
            }
        )
    return int(total[0] or 0), npcs


def source_npcs_for_item_names(s, item_names, *, per_item_limit: int) -> dict[str, int]:
    """``{npc_name: npc_id}`` for every NPC that drops any of ``item_names``.

    The effort resolver's inference step. Each name is capped at
    ``per_item_limit`` sources (rarity-ranked, so the rarest — i.e. most
    boss-like — win) because a single common item can name hundreds of NPCs
    and an event's effort set has to stay small enough to check per submission.
    """
    out: dict[str, int] = {}
    for name in item_names:
        ids = item_ids_for_name(s, name)
        if not ids:
            continue
        _total, rows = source_npc_rows(s, ids, limit=per_item_limit)
        for row in rows:
            out.setdefault(row["name"], row["npc_id"])
    return out


def items_dropped_by_npcs(s, npc_names, candidate_item_names) -> dict[str, list[str]]:
    """``{candidate item name: [NPC names that drop it]}`` — the intersection
    of a small candidate item list with a small NPC list, in one round trip.

    The inverse question to :func:`source_npc_rows`, and asked from the other
    end: the caller already knows both short lists and only wants to know which
    pairs exist. Used to answer "which pets does this Boss of the Week actually
    drop?" at authoring time, so a bonus rule's pet list is real drop-table
    data rather than a hand-maintained map that goes stale every game update.

    Wiki rows only (``dt_npc_loot``): a pet's observed-drops fallback would let
    one misattributed receipt attach a pet to the wrong boss, and unlike the
    item page there is no human reading the result — it is written straight
    into a scoring config. Names are matched exactly, as stored.
    """
    wanted = [str(n).strip() for n in (candidate_item_names or []) if str(n).strip()]
    npcs = [str(n).strip() for n in (npc_names or []) if str(n).strip()]
    if not wanted or not npcs:
        return {}
    rows = s.execute(
        text(
            "SELECT DISTINCT i.item_name, n.npc_name "
            "FROM xenforo.dt_npc_loot l "
            "JOIN npc_list n ON n.npc_id = l.npc_id "
            "JOIN items i ON i.item_id = l.item_id "
            "WHERE n.npc_name IN :npcs AND i.item_name IN :items"
        ).bindparams(bindparam("npcs", expanding=True),
                     bindparam("items", expanding=True)),
        {"npcs": npcs, "items": wanted},
    ).fetchall()
    out: dict[str, list[str]] = {}
    for item_name, npc_name in rows:
        bucket = out.setdefault(str(item_name), [])
        if npc_name not in bucket:
            bucket.append(str(npc_name))
    return out
