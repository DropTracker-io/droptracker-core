"""Public item pages (the web front-end's `/items/{id}`).

  GET /api/v1/items/<item_id>  (public, cached)

Everything the site knows about one item: identity + GE value, lifetime /
current-month receive totals (from ``player_item_hourly_totals``, which covers
the full drops history), the players who received it most recently, all-time
top receivers, and which NPCs drop it (wiki table ``xenforo.dt_npc_loot``).

Stats cost: the hourly-totals scan is a single (item_id) index range, but the
value columns live on the row, so ubiquitous items pay millions of row lookups
(~15s for Coins). Those are computed once per _STATS_TTL; for items above
_INLINE_STATS_MAX_ROWS the cold compute runs in a background task and the
endpoint reports ``stats_status: "building"`` instead of blocking the request.
"""
from __future__ import annotations

import asyncio
import time

from quart import Blueprint, jsonify
from sqlalchemy import bindparam, text

from db import ItemList, Player
from db.item_sources import (
    SOURCES_LIMIT as _SOURCES_LIMIT,
    observed_source_rows as _observed_source_rows,
    source_npc_rows,
    variant_item_ids,
)
from utils.ge_value import get_true_item_value
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

items_bp = Blueprint("v1_items", __name__)

IMG_BASE = "https://www.droptracker.io/img"
WIKI_BASE = "https://oldschool.runescape.wiki/w"

_STATS_TTL = 1800.0  # heavy aggregate (worst observed compute ~15s for Coins)
_RECENT_TTL = 60.0
_SOURCES_TTL = 3600.0

# Items whose hourly-totals row count exceeds this get their cold stats build
# deferred to a background task (row count itself is index-covered → cheap).
_INLINE_STATS_MAX_ROWS = 60_000
_STATS_LOCK_KEY = "item:stats:{item_id}:building"
_STATS_LOCK_TTL = 300

_RECENT_LIMIT = 15
_TOP_RECEIVERS_LIMIT = 10

# The source queries themselves live in ``db/item_sources.py`` — the events
# worker needs the same answer for effort attribution and must not import a
# route module. Imported above under their historical names; this module keeps
# only the presentation layer (icons, alias collapsing, caching).


def _rc():
    return getattr(redis_client, "client", None)


def _item_or_404(item_id: int, s) -> ItemList:
    item = s.query(ItemList).filter(ItemList.item_id == item_id).first()
    if item is None:
        abort_problem(404, "Unknown item", f"No item with id {item_id}.")
    return item


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
# Aggregate stats (single pass over player_item_hourly_totals, cached).       #
# --------------------------------------------------------------------------- #
def _compute_stats(item_id: int) -> dict:
    hidden = hidden_player_ids()
    partition = get_current_partition()
    with db_session() as s:
        # Aggregate in SQL rather than hydrating every per-player group into
        # Python. Backed by the covering index idx_item_stats_covering
        # (item_id, player_id, partition, total_value, quantity, drop_count,
        # last_drop_time), all three reads are index-only — ubiquitous items
        # (Coins: 800k+ hourly rows) went from millions of row lookups / 25s+
        # (read-timeout → the failing background stats build) to a ~1s index
        # scan. Mirrors `_npc_stats`.
        lifetime = s.execute(
            text(
                "SELECT COALESCE(SUM(total_value),0), COALESCE(SUM(quantity),0), "
                "       COALESCE(SUM(drop_count),0), COUNT(DISTINCT player_id), "
                "       MAX(last_drop_time) "
                "FROM player_item_hourly_totals WHERE item_id = :iid"
            ),
            {"iid": item_id},
        ).fetchone()
        month = s.execute(
            text(
                "SELECT COALESCE(SUM(total_value),0), COALESCE(SUM(quantity),0), "
                "       COALESCE(SUM(drop_count),0), COUNT(DISTINCT player_id) "
                "FROM player_item_hourly_totals WHERE item_id = :iid AND `partition` = :part"
            ),
            {"iid": item_id, "part": partition},
        ).fetchone()
        top_rows = s.execute(
            text(
                "SELECT player_id, SUM(total_value) AS v, SUM(quantity) AS q, "
                "       SUM(drop_count) AS c "
                "FROM player_item_hourly_totals WHERE item_id = :iid "
                "GROUP BY player_id ORDER BY v DESC LIMIT :lim"
            ),
            {"iid": item_id, "lim": _TOP_RECEIVERS_LIMIT + len(hidden)},
        ).fetchall()

        top = [r for r in top_rows if int(r[0]) not in hidden][:_TOP_RECEIVERS_LIMIT]
        names = _player_names(s, {int(r[0]) for r in top})

    total_value = int(lifetime[0] or 0)
    total_qty = int(lifetime[1] or 0)
    total_count = int(lifetime[2] or 0)
    unique_players = int(lifetime[3] or 0)
    try:
        last_ts = int(lifetime[4].timestamp()) if lifetime[4] else None
    except Exception:
        last_ts = None

    return {
        "lifetime": {
            "loot": money(total_value),
            "quantity": total_qty,
            "drop_count": total_count,
            "unique_players": unique_players,
            "last_drop_ts": last_ts,
        },
        "month": {
            "partition": partition,
            "loot": money(month[0]),
            "quantity": int(month[1] or 0),
            "drop_count": int(month[2] or 0),
            "unique_players": int(month[3] or 0),
        },
        "top_receivers": [
            {
                "rank": i + 1,
                "player_id": int(pid),
                "player_name": names.get(int(pid), "Unknown"),
                "loot": money(int(v or 0)),
                "quantity": int(q or 0),
                "drop_count": int(c or 0),
            }
            for i, (pid, v, q, c) in enumerate(top)
        ],
    }


def _stats_row_count(s, item_id: int) -> int:
    """Index-covered row count — cheap even for Coins-scale items."""
    row = s.execute(
        text("SELECT COUNT(*) FROM player_item_hourly_totals WHERE item_id = :iid"),
        {"iid": item_id},
    ).fetchone()
    return int(row[0] or 0)


def _build_stats_blocking(item_id: int) -> None:
    cache_set(f"item:stats:{item_id}", _compute_stats(item_id))


async def _build_stats_background(item_id: int) -> None:
    conn = _rc()
    lock_key = _STATS_LOCK_KEY.format(item_id=item_id)
    if conn is not None:
        try:
            if not conn.set(lock_key, str(int(time.time())), nx=True, ex=_STATS_LOCK_TTL):
                return
        except Exception:
            pass
    try:
        await asyncio.to_thread(_build_stats_blocking, item_id)
    finally:
        if conn is not None:
            try:
                conn.delete(lock_key)
            except Exception:
                pass


def _stats_for(s, item_id: int) -> tuple[dict | None, str]:
    """(stats or None, "ready" | "building")."""
    cached = cache_get(f"item:stats:{item_id}", _STATS_TTL)
    if cached is not None:
        return cached, "ready"
    if _stats_row_count(s, item_id) <= _INLINE_STATS_MAX_ROWS:
        stats = _compute_stats(item_id)
        cache_set(f"item:stats:{item_id}", stats)
        return stats, "ready"
    return None, "building"


# --------------------------------------------------------------------------- #
# Cheap sections                                                              #
# --------------------------------------------------------------------------- #
def _recent_drops(item_id: int) -> list:
    """Latest tracked receipts of this item ((item_id, drop_id) index scan)."""
    key = f"item:recent:{item_id}"
    cached = cache_get(key, _RECENT_TTL)
    if cached is not None:
        return cached

    hidden = hidden_player_ids()
    with db_session() as s:
        rows = s.execute(
            text(
                "SELECT d.drop_id, d.npc_id, n.npc_name, d.player_id, d.value, "
                "       d.quantity, d.date_added "
                "FROM drops d LEFT JOIN npc_list n ON n.npc_id = d.npc_id "
                "WHERE d.item_id = :iid ORDER BY d.drop_id DESC LIMIT :lim"
            ),
            {"iid": item_id, "lim": _RECENT_LIMIT * 2},
        ).fetchall()
        rows = [r for r in rows if int(r[3]) not in hidden][:_RECENT_LIMIT]
        names = _player_names(s, {int(r[3]) for r in rows})

    out = []
    for drop_id, npc_id, npc_name, player_id, value, quantity, date_added in rows:
        try:
            ts = int(date_added.timestamp()) if date_added else 0
        except Exception:
            ts = 0
        out.append(
            {
                "drop_id": int(drop_id),
                "npc_id": int(npc_id) if npc_id else None,
                "npc_name": npc_name,
                "npc_icon_url": f"{IMG_BASE}/npcdb/{npc_id}.png" if npc_id else None,
                "player_id": int(player_id),
                "player_name": names.get(int(player_id), "Unknown"),
                "value": money((value or 0) * (quantity or 1)),
                "quantity": int(quantity or 1),
                "ts": ts,
            }
        )
    cache_set(key, out)
    return out


def _sources(item_ids: list[int]) -> dict:
    """NPCs that drop this item, ready for display: the shared source rows
    (``db.item_sources.source_npc_rows``) with icons attached and alias groups
    (e.g. Wintertodt's two reward containers) collapsed to one entry.

    Takes EVERY ``items`` id the item's name maps to, not one — see
    :func:`db.item_sources.source_npc_rows` for why.
    """
    from web_api.routes.npc_source_aliases import alias_group_for_member

    ids = sorted({int(i) for i in item_ids})
    key = "item:sources:v2:" + ",".join(str(i) for i in ids)
    cached = cache_get(key, _SOURCES_TTL)
    if cached is not None:
        return cached

    with db_session() as s:
        wiki_total, rows = source_npc_rows(s, ids, limit=_SOURCES_LIMIT)

    # The wiki-gap fallback entries aren't in wiki_total, so they're added back
    # below; `observed` is a resolver detail and never reaches the payload.
    extra = sum(1 for row in rows if row.get("observed"))
    npcs = [
        {
            **{k: v for k, v in row.items() if k != "observed"},
            "icon_url": f"{IMG_BASE}/npcdb/{row['npc_id']}.png",
        }
        for row in rows
    ]
    # Collapse alias-group members ("Reward cart (Wintertodt)" + "Supply crate
    # (Wintertodt)") into one display entry. `members` carries the real names so
    # the source-restriction picker stores what drops actually record.
    # `member_ids` is the same list for id-keyed callers (the points
    # include/exclude lists store an npc id, not a name): the ids of the rows
    # that merged, NOT the alias's representative id, which only exists to pick
    # an icon and may name an npc no drop is ever recorded under.
    member_ids: dict[str, list[int]] = {}
    for entry in npcs:
        group = alias_group_for_member(entry["name"])
        if group is not None:
            member_ids.setdefault(group["name"], []).append(int(entry["npc_id"]))
    collapsed, seen_alias = [], set()
    for entry in npcs:
        group = alias_group_for_member(entry["name"])
        if group is None:
            collapsed.append(entry)
            continue
        if group["name"] in seen_alias:
            extra -= 1  # merged away
            continue
        seen_alias.add(group["name"])
        collapsed.append(
            {
                **entry,
                "npc_id": int(group["npc_id"]),
                "name": group["name"],
                "icon_url": f"{IMG_BASE}/npcdb/{group['npc_id']}.png",
                "members": list(group["members"]),
                "member_ids": member_ids.get(group["name"], []),
            }
        )
    out = {"total": int(wiki_total) + max(extra, 0), "npcs": collapsed}
    cache_set(key, out)
    return out


# --------------------------------------------------------------------------- #
# Route                                                                       #
# --------------------------------------------------------------------------- #
@items_bp.get("/items/<int:item_id>")
async def item_detail(item_id: int):
    def _load():
        with db_session() as s:
            item = _item_or_404(item_id, s)
            name = item.item_name or f"Item {item_id}"
            stackable = bool(item.stackable)
            stats, stats_status = _stats_for(s, item_id)
            # Drop sources are keyed by item id but recorded against whichever
            # variant id the client sent, so ask about the whole name -> ids
            # set (see _sources); landing on a "dead" variant id must not empty
            # the drop-source list.
            source_ids = variant_item_ids(s, item.item_name, item_id)
        return {
            "item_id": item_id,
            "name": name,
            "icon_url": f"{IMG_BASE}/itemdb/{item_id}.png",
            "wiki_url": f"{WIKI_BASE}/{name.replace(' ', '_')}",
            # Item name variants collapse to a primary id, so the slug is always
            # canonical (no session needed for npc/item). None only if unslugifiable.
            "canonical_slug": canonical_slug_for(None, "item", item_id, name),
            "stackable": stackable,
            "lifetime": stats["lifetime"] if stats else None,
            "month": stats["month"] if stats else None,
            "top_receivers": stats["top_receivers"] if stats else [],
            "stats_status": stats_status,
            "recent_drops": _recent_drops(item_id),
            "sources": _sources(source_ids),
        }

    payload = await asyncio.to_thread(_load)

    # Live GE price via the shared Redis-cached wiki-prices helper (only hits
    # the external API on cache miss; never fails the page).
    try:
        price = await get_true_item_value(payload["name"], item_id=item_id)
        payload["ge_value"] = money(price) if price else None
    except Exception:
        payload["ge_value"] = None

    if payload["stats_status"] == "building":
        asyncio.get_running_loop().create_task(_build_stats_background(item_id))
    max_age = 15 if payload["stats_status"] == "building" else 120
    return with_cache_headers(jsonify(payload), max_age=max_age)
