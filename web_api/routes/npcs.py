"""Public NPC pages (the web front-end's `/npcs/{id}`).

  GET /api/v1/npcs/<npc_id>             (public, cached)
  GET /api/v1/npcs/<npc_id>/drop-table  (public, cached)

Replaces the old XenForo "npc_view" page: header stats (lifetime GP, times
looted, unique players), all-time top players, recent tracked drops, and the
wiki drop table (from ``xenforo.dt_npc_loot``) annotated with the player who
most recently received each item from this NPC.

Last-received design (the `drops` table holds 167M+ rows, and `item_id` is
not part of `ix_drops_npc_id`, so a per-NPC ``GROUP BY item_id`` does millions
of row lookups — ~2 minutes for the busiest NPCs):

  * A per-NPC Redis hash (``npc:{id}:last_item_drops``) maps item_id → the
    latest drop's {drop_id, player_id, ts, value, quantity}. A ``_cursor``
    field stores the highest drop_id scanned.
  * Requests top the hash up incrementally: ``WHERE npc_id = ? AND drop_id >
    cursor`` is a cheap (npc_id, drop_id) index range scan.
  * A cold registry is built inline only for small NPCs
    (<= _INLINE_BUILD_MAX_DROPS tracked drops); bigger ones are built once in
    a background task while the endpoint reports ``last_drops_status:
    "building"`` — ``scripts/backfill_npc_last_drops.py`` pre-seeds every NPC
    so this path is rare in practice.
"""
from __future__ import annotations

import asyncio
import json
import time

from quart import Blueprint, jsonify, request
from sqlalchemy import text

from db import NpcList, Player
from utils.npc_names import npc_family_tiers, npc_slug_sql_expr
from utils.redis import redis_client
from web_api.common import (
    abort_problem,
    cache_get,
    cache_set,
    canonical_slug_for,
    db_session,
    get_current_partition,
    hidden_player_ids,
    money,
    with_cache_headers,
)

npcs_bp = Blueprint("v1_npcs", __name__)

IMG_BASE = "https://www.droptracker.io/img"
WIKI_BASE = "https://oldschool.runescape.wiki/w"

_STATS_TTL = 300.0  # per-NPC aggregate stats (worst observed compute ~1.5s)
_RECENT_TTL = 60.0

# Redis registry keys for the per-(npc, item) last-received drops.
_LAST_DROPS_KEY = "npc:{npc_id}:last_item_drops"
_LAST_DROPS_CURSOR_FIELD = "_cursor"
_LAST_DROPS_LOCK_KEY = "npc:{npc_id}:last_item_drops:building"
_LAST_DROPS_LOCK_TTL = 600  # seconds; guards against duplicate cold builds

# Cold registries are built inline only for NPCs with at most this many
# tracked drops (~1s of row lookups); above it the build runs in a background
# task and the endpoint reports "building" until it lands.
_INLINE_BUILD_MAX_DROPS = 50_000

_TOP_PLAYERS_LIMIT = 10
_RECENT_DROPS_LIMIT = 15


def _rc():
    return getattr(redis_client, "client", None)


def _npc_or_404(npc_id: int, s) -> NpcList:
    npc = s.query(NpcList).filter(NpcList.npc_id == npc_id).first()
    if npc is None:
        abort_problem(404, "Unknown NPC", f"No NPC with id {npc_id}.")
    return npc


def _wiki_url(npc_name: str) -> str:
    return f"{WIKI_BASE}/{npc_name.replace(' ', '_')}"


def _player_names(s, ids: set) -> dict:
    if not ids:
        return {}
    rows = (
        s.query(Player.player_id, Player.player_name)
        .filter(Player.player_id.in_(ids))
        .all()
    )
    return {int(pid): name for pid, name in rows}


# --------------------------------------------------------------------------- #
# Aggregate stats (player_npc_hourly_totals covers the full drops history).   #
# --------------------------------------------------------------------------- #
def _npc_stats(npc_id: int) -> dict:
    """Lifetime + current-month totals and all-time top players, cached."""
    key = f"npc:stats:{npc_id}"
    cached = cache_get(key, _STATS_TTL)
    if cached is not None:
        return cached

    hidden = hidden_player_ids()
    partition = get_current_partition()
    with db_session() as s:
        lifetime = s.execute(
            text(
                "SELECT COALESCE(SUM(total_value),0), COALESCE(SUM(drop_count),0), "
                "       COUNT(DISTINCT player_id), MAX(last_drop_time) "
                "FROM player_npc_hourly_totals WHERE npc_id = :nid"
            ),
            {"nid": npc_id},
        ).fetchone()
        month = s.execute(
            text(
                "SELECT COALESCE(SUM(total_value),0), COALESCE(SUM(drop_count),0), "
                "       COUNT(DISTINCT player_id) "
                "FROM player_npc_hourly_totals WHERE npc_id = :nid AND `partition` = :part"
            ),
            {"nid": npc_id, "part": partition},
        ).fetchone()
        top_rows = s.execute(
            text(
                "SELECT player_id, SUM(total_value) AS v, SUM(drop_count) AS c "
                "FROM player_npc_hourly_totals WHERE npc_id = :nid "
                "GROUP BY player_id ORDER BY v DESC LIMIT :lim"
            ),
            {"nid": npc_id, "lim": _TOP_PLAYERS_LIMIT + len(hidden)},
        ).fetchall()

        top_rows = [r for r in top_rows if int(r[0]) not in hidden][:_TOP_PLAYERS_LIMIT]
        names = _player_names(s, {int(r[0]) for r in top_rows})

    try:
        last_drop_ts = int(lifetime[3].timestamp()) if lifetime[3] else None
    except Exception:
        last_drop_ts = None

    stats = {
        "lifetime": {
            "loot": money(lifetime[0]),
            "drop_count": int(lifetime[1] or 0),
            "unique_players": int(lifetime[2] or 0),
            "last_drop_ts": last_drop_ts,
        },
        "month": {
            "partition": partition,
            "loot": money(month[0]),
            "drop_count": int(month[1] or 0),
            "unique_players": int(month[2] or 0),
        },
        "top_players": [
            {
                "rank": i + 1,
                "player_id": int(pid),
                "player_name": names.get(int(pid), "Unknown"),
                "loot": money(v),
                "drop_count": int(c or 0),
            }
            for i, (pid, v, c) in enumerate(top_rows)
        ],
    }
    cache_set(key, stats)
    return stats


def _recent_drops(npc_id: int) -> list:
    """Latest tracked drops from this NPC (cheap (npc_id, drop_id) scan)."""
    key = f"npc:recent:{npc_id}"
    cached = cache_get(key, _RECENT_TTL)
    if cached is not None:
        return cached

    hidden = hidden_player_ids()
    with db_session() as s:
        rows = s.execute(
            text(
                "SELECT d.drop_id, d.item_id, i.item_name, d.player_id, d.value, "
                "       d.quantity, d.date_added "
                "FROM drops d LEFT JOIN items i ON i.item_id = d.item_id "
                "WHERE d.npc_id = :nid ORDER BY d.drop_id DESC LIMIT :lim"
            ),
            {"nid": npc_id, "lim": _RECENT_DROPS_LIMIT * 2},
        ).fetchall()
        rows = [r for r in rows if int(r[3]) not in hidden][:_RECENT_DROPS_LIMIT]
        names = _player_names(s, {int(r[3]) for r in rows})

    out = []
    for drop_id, item_id, item_name, player_id, value, quantity, date_added in rows:
        try:
            ts = int(date_added.timestamp()) if date_added else 0
        except Exception:
            ts = 0
        out.append(
            {
                "drop_id": int(drop_id),
                "item_id": int(item_id) if item_id else None,
                "item_name": item_name or (f"Item {item_id}" if item_id else "Unknown item"),
                "icon_url": f"{IMG_BASE}/itemdb/{item_id}.png" if item_id else None,
                "player_id": int(player_id),
                "player_name": names.get(int(player_id), "Unknown"),
                "value": money((value or 0) * (quantity or 1)),
                "quantity": int(quantity or 1),
                "ts": ts,
            }
        )
    cache_set(key, out)
    return out


# --------------------------------------------------------------------------- #
# Last-received registry (Redis hash, incrementally topped up per request).   #
# --------------------------------------------------------------------------- #
def _scan_last_drops(s, npc_id: int, since_drop_id: int) -> tuple[dict, int]:
    """Latest drop per item for this NPC above ``since_drop_id``.

    Returns ({item_id: entry}, max_drop_id_seen). Uses the implicit
    (npc_id, drop_id) ordering of ``ix_drops_npc_id`` so incremental scans
    only touch rows newer than the cursor.
    """
    rows = s.execute(
        text(
            "SELECT drop_id, item_id, player_id, value, quantity, date_added "
            "FROM drops WHERE npc_id = :nid AND drop_id > :cur"
        ),
        {"nid": npc_id, "cur": since_drop_id},
    ).fetchall()
    latest: dict[int, dict] = {}
    max_id = since_drop_id
    for drop_id, item_id, player_id, value, quantity, date_added in rows:
        drop_id = int(drop_id)
        if drop_id > max_id:
            max_id = drop_id
        if item_id is None or player_id is None:
            continue
        item_id = int(item_id)
        cur = latest.get(item_id)
        if cur is None or drop_id > cur["drop_id"]:
            try:
                ts = int(date_added.timestamp()) if date_added else 0
            except Exception:
                ts = 0
            latest[item_id] = {
                "drop_id": drop_id,
                "player_id": int(player_id),
                "ts": ts,
                "value": int(value or 0),
                "quantity": int(quantity or 1),
            }
    return latest, max_id


def _store_last_drops(npc_id: int, latest: dict, cursor: int) -> None:
    conn = _rc()
    if conn is None or cursor <= 0:
        return
    key = _LAST_DROPS_KEY.format(npc_id=npc_id)
    try:
        mapping = {str(item_id): json.dumps(entry) for item_id, entry in latest.items()}
        mapping[_LAST_DROPS_CURSOR_FIELD] = str(cursor)
        conn.hset(key, mapping=mapping)
    except Exception:
        pass


def _load_last_drops(npc_id: int) -> tuple[dict, int | None]:
    """Read the registry hash → ({item_id: entry}, cursor or None if cold)."""
    conn = _rc()
    if conn is None:
        return {}, None
    key = _LAST_DROPS_KEY.format(npc_id=npc_id)
    try:
        raw = conn.hgetall(key)
    except Exception:
        return {}, None
    if not raw:
        return {}, None
    cursor = None
    latest: dict[int, dict] = {}
    for field, value in raw.items():
        field = field.decode() if isinstance(field, bytes) else str(field)
        value = value.decode() if isinstance(value, bytes) else str(value)
        if field == _LAST_DROPS_CURSOR_FIELD:
            try:
                cursor = int(value)
            except (TypeError, ValueError):
                cursor = None
            continue
        try:
            latest[int(field)] = json.loads(value)
        except Exception:
            continue
    return latest, cursor


def _npc_drop_volume(s, npc_id: int) -> int:
    row = s.execute(
        text("SELECT COALESCE(SUM(drop_count),0) FROM player_npc_hourly_totals WHERE npc_id = :nid"),
        {"nid": npc_id},
    ).fetchone()
    return int(row[0] or 0)


def _build_registry_blocking(npc_id: int) -> dict:
    """Full cold build (may take minutes for the busiest NPCs)."""
    with db_session() as s:
        latest, cursor = _scan_last_drops(s, npc_id, 0)
    if cursor > 0:
        _store_last_drops(npc_id, latest, cursor)
    return latest


async def _build_registry_background(npc_id: int) -> None:
    conn = _rc()
    lock_key = _LAST_DROPS_LOCK_KEY.format(npc_id=npc_id)
    if conn is not None:
        try:
            # NX lock so only one worker builds; TTL self-clears crashed builds.
            if not conn.set(lock_key, str(int(time.time())), nx=True, ex=_LAST_DROPS_LOCK_TTL):
                return
        except Exception:
            pass
    try:
        await asyncio.to_thread(_build_registry_blocking, npc_id)
    finally:
        if conn is not None:
            try:
                conn.delete(lock_key)
            except Exception:
                pass


def _last_drops_for(s, npc_id: int) -> tuple[dict, str]:
    """({item_id: entry}, status) where status is "ready" or "building".

    Warm registries are topped up inline (cheap range scan since the cursor).
    Cold ones are built inline when the NPC is small, otherwise deferred to a
    background task and reported as "building".
    """
    latest, cursor = _load_last_drops(npc_id)
    if cursor is not None:
        new, max_id = _scan_last_drops(s, npc_id, cursor)
        if max_id > cursor:
            latest.update(new)
            _store_last_drops(npc_id, new, max_id)
        return latest, "ready"

    if _npc_drop_volume(s, npc_id) <= _INLINE_BUILD_MAX_DROPS:
        latest, max_id = _scan_last_drops(s, npc_id, 0)
        # Cursor 0 is fine for empty NPCs — the next request rescans nothing.
        _store_last_drops(npc_id, latest, max_id if max_id > 0 else 1)
        return latest, "ready"

    return {}, "building"


# --------------------------------------------------------------------------- #
# Routes                                                                      #
# --------------------------------------------------------------------------- #
@npcs_bp.get("/npcs/<int:npc_id>")
async def npc_detail(npc_id: int):
    """NPC overview: identity, lifetime/month totals, top players, recent drops."""

    def _load():
        with db_session() as s:
            npc = _npc_or_404(npc_id, s)
            name = npc.npc_name
        stats = _npc_stats(npc_id)
        return {
            "npc_id": npc_id,
            "name": name,
            "icon_url": f"{IMG_BASE}/npcdb/{npc_id}.png",
            "wiki_url": _wiki_url(name),
            # Duplicate npc names collapse to a primary id, so the slug is always
            # canonical (no session needed for npc/item). None only if unslugifiable.
            "canonical_slug": canonical_slug_for(None, "npc", npc_id, name),
            "lifetime": stats["lifetime"],
            "month": stats["month"],
            "top_players": stats["top_players"],
            "recent_drops": _recent_drops(npc_id),
        }

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=60)


def _wiki_table_rows(s, nid: int):
    return s.execute(
        text(
            "SELECT l.item_id, COALESCE(i.item_name, CONCAT('Item ', l.item_id)), "
            "       l.quantity, l.noted, l.rarity, l.rolls "
            "FROM xenforo.dt_npc_loot l "
            "LEFT JOIN items i ON i.item_id = l.item_id "
            "WHERE l.npc_id = :nid ORDER BY l.rarity DESC, l.item_id ASC"
        ),
        {"nid": nid},
    ).fetchall()


def _family_table_rows(s, npc_id: int, npc_name: str):
    """Wiki rows from the nearest boss-family donor when this npc has none.

    The importer landed each family's table on one arbitrary member (CoX table
    on the base raid, ToB's on Hard Mode), so mode/spelling/article/alias
    variants render empty without this. Priority tiers: same boss (spelling,
    "The ", alias variants) → base raid → mode siblings (suggestion #50).
    """
    from sqlalchemy import bindparam

    expr = npc_slug_sql_expr("npc_name")
    sql = text(
        f"SELECT npc_id FROM npc_list "
        f"WHERE {expr} IN :slugs AND npc_id <> :nid ORDER BY npc_id ASC"
    ).bindparams(bindparam("slugs", expanding=True))
    for tier in npc_family_tiers(npc_name):
        for (sid,) in s.execute(sql, {"slugs": tier, "nid": npc_id}).fetchall():
            rows = _wiki_table_rows(s, int(sid))
            if rows:
                return rows
    return []


@npcs_bp.get("/npcs/<int:npc_id>/drop-table")
async def npc_drop_table(npc_id: int):
    """Wiki drop table + who most recently received each item from this NPC."""

    def _load():
        with db_session() as s:
            npc_name = _npc_or_404(npc_id, s).npc_name
            rows = _wiki_table_rows(s, npc_id)
            if not rows:
                rows = _family_table_rows(s, npc_id, npc_name)

            # No wiki table → nothing to annotate; skip the registry entirely
            # (a cold build on a busy NPC is minutes of scanning for nothing).
            last_drops, status = _last_drops_for(s, npc_id) if rows else ({}, "ready")
            hidden = hidden_player_ids()
            names = _player_names(
                s,
                {
                    e["player_id"]
                    for e in last_drops.values()
                    if e.get("player_id") and e["player_id"] not in hidden
                },
            )

        items = []
        # dt_npc_loot carries repeated-import duplicates (identical tuples up
        # to 60×: CoX 561 rows / 51 items) — collapse them; distinct tuples
        # for one item (e.g. two legit rarity tiers) are kept.
        seen_rows = set()
        for item_id, item_name, quantity, noted, rarity, rolls in rows:
            item_id = int(item_id)
            row_key = (item_id, str(quantity), bool(noted), float(rarity), int(rolls or 1))
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            entry = last_drops.get(item_id)
            last = None
            if entry and entry.get("player_id") and entry["player_id"] not in hidden:
                last = {
                    "player_id": entry["player_id"],
                    "player_name": names.get(entry["player_id"], "Unknown"),
                    "ts": entry.get("ts") or 0,
                    "value": money(entry.get("value", 0) * entry.get("quantity", 1)),
                }
            items.append(
                {
                    "item_id": item_id,
                    "name": item_name,
                    "icon_url": f"{IMG_BASE}/itemdb/{item_id}.png",
                    "quantity": str(quantity),
                    "noted": bool(noted),
                    "rarity": float(rarity),
                    "rolls": int(rolls or 1),
                    "last_drop": last,
                }
            )

        return {
            "npc_id": npc_id,
            "name": npc_name,
            "items": items,
            "last_drops_status": status,
        }

    payload = await asyncio.to_thread(_load)
    if payload["last_drops_status"] == "building":
        # Fire the cold build once; subsequent requests see "ready".
        asyncio.get_running_loop().create_task(_build_registry_background(npc_id))
    # Short cache while building so clients pick the annotations up quickly.
    max_age = 15 if payload["last_drops_status"] == "building" else 120
    return with_cache_headers(jsonify(payload), max_age=max_age)
