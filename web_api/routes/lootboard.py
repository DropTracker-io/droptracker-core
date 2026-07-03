"""Task 13 — native lootboard data.

  GET  /api/v1/groups/{id}/lootboard?period=          (public, cached) -> Lootboard
  POST /api/v1/groups/{id}/lootboard/generate          -> { url }  (wraps the PNG CLI)

The JSON board mirrors the legacy PIL generator (``disc/lootboard/generator.py``)
so the web can render the exact same board — the group's configured template
plus the item grid, leaderboard, recent drops and header — as an interactive
HTML overlay instead of a PNG. It is aggregated from the same per-player Redis
keys that feed the PNG generator, so no image/PIL dependency is pulled into the
read path. Hidden (ignored) players are excluded.

Redis keys (per member, ``token`` from ``resolve_period``):
  ``player:{id}:{token}:total_items``  hash item_id -> "qty,value,count,first,last"
  ``player:{id}:{token}:total_loot``   string GP  (leaderboard)
  ``player:{id}:{token}:recent_items`` list of JSON drops (recent-drops panel)
"""
from __future__ import annotations

import asyncio
import calendar
import json
import os

import httpx
from quart import Blueprint, jsonify, request

from db import Group, GroupConfiguration, IgnoredPlayer, ItemList, LootboardStyle, Player
from web_api.common import (
    abort_problem,
    db_session,
    money,
    resolve_period,
    with_cache_headers,
    _rc,
)

lootboard_bp = Blueprint("v1_lootboard", __name__)

IMG_BASE = "https://www.droptracker.io/img"
INTAKE_API_URL = os.getenv("INTAKE_API_URL", "http://127.0.0.1:31323")

# The PNG generator paints onto a fixed 1074x795 canvas and shows the top 32
# items, top 12 recent drops and top 12 leaderboard players. The web overlay
# uses the same slots/coordinates, so we cap to match.
CANVAS = {"width": 1074, "height": 795}
MAX_ITEMS = 32
MAX_RECENT = 12
MAX_LEADERBOARD = 12
DEFAULT_MIN_VALUE = 2_500_000

# Filesystem prefix shared by every ``LootboardStyle.local_url`` row. The
# matching directory is exposed over the image server via the symlink
# ``static/assets/img/lootboard -> ../../lootboard`` (see lootboard route docs).
_THEME_PREFIX = "/store/droptracker/disc/lootboard/"
_DEFAULT_THEME_URL = f"{IMG_BASE}/lootboard/bank-new-clean-dark.png"

# Coin (item 995) icon variant by quantity — mirrors utils.dynamic_handling.get_coin_image_id.
_COIN_VARIANTS = {1: 995, 2: 996, 3: 997, 4: 998, 5: 999, 10: 1000, 50: 1001, 100: 1002, 1000: 1003, 10000: 1004}


def _coin_image_id(quantity: int) -> int:
    possible = [k for k in _COIN_VARIANTS if quantity >= k]
    return _COIN_VARIANTS[max(possible)] if possible else _COIN_VARIANTS[1]


def _player_key(player_id: int, token: str, suffix: str) -> str:
    """Per-player Redis key for ``suffix`` (total_items|total_loot|recent_items).

    Matches ``services/redis_updates`` layout. Weekly has no per-partition hash,
    so it falls back to the current month (documented limitation)."""
    if token == "all":
        return f"player:{player_id}:all:{suffix}"
    if len(token) == 8 and token.isdigit():  # YYYYMMDD
        return f"player:{player_id}:daily:{token}:{suffix}"
    if "W" in token:  # weekly -> fall back to current month
        from utils.partitions import month_token

        return f"player:{player_id}:{month_token()}:{suffix}"
    return f"player:{player_id}:{token}:{suffix}"  # YYYYMM


def _item_hash_key(player_id: int, token: str) -> str:
    return _player_key(player_id, token, "total_items")


def _parse_item_value(raw) -> tuple[int, int]:
    """Return (quantity, total_value) from a ``qty,value,count,first,last`` blob."""
    try:
        s = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        parts = s.split(",")
        return int(float(parts[0])), int(float(parts[1]))
    except Exception:
        return 0, 0


def _as_str(raw) -> str:
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)


def _truthy(value, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _theme_url(local_url: str | None) -> str:
    """Map a ``LootboardStyle.local_url`` filesystem path to an image-server URL."""
    if local_url and local_url.startswith(_THEME_PREFIX):
        rel = local_url[len(_THEME_PREFIX):].lstrip("/")
        return f"{IMG_BASE}/lootboard/{rel}"
    return _DEFAULT_THEME_URL


def _period_label(token: str) -> str:
    """Human date shown in the header, mirroring generator.draw_headers."""
    if token == "all":
        return "All Time"
    if len(token) == 6 and token.isdigit():  # YYYYMM
        return calendar.month_name[int(token[4:6])]
    if len(token) == 8 and token.isdigit():  # YYYYMMDD
        return f"{calendar.month_name[int(token[4:6])]} {int(token[6:8])}, {token[:4]}"
    if "W" in token:
        return "This Week"
    return token


@lootboard_bp.get("/groups/<int:group_id>/lootboard")
async def group_lootboard(group_id: int):
    period = request.args.get("period", "all")
    token = resolve_period(period)

    def _load():
        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                return None

            # --- Group config (theme, min value, colours) — same keys the PIL
            # generator reads (disc/lootboard/generator.py). ---
            config = {
                c.config_key: c.config_value
                for c in s.query(GroupConfiguration)
                .filter(GroupConfiguration.group_id == group_id)
                .all()
            }
            try:
                min_value = int(config.get("minimum_value_to_notify") or DEFAULT_MIN_VALUE)
            except Exception:
                min_value = DEFAULT_MIN_VALUE
            only_over_min = _truthy(config.get("only_include_items_over_minimum"), default=False)
            use_gp_colors = _truthy(config.get("use_gp_colors"), default=True)

            try:
                style_id = int(config.get("loot_board_type") or 1) or 1
            except Exception:
                style_id = 1
            style = s.query(LootboardStyle).filter(LootboardStyle.id == style_id).first()
            if not style:
                style = s.query(LootboardStyle).filter(LootboardStyle.id == 1).first()
            background_url = _theme_url(style.local_url if style else None)

            hidden = {
                pid for (pid,) in s.query(IgnoredPlayer.player_id)
                .filter(IgnoredPlayer.group_id == group_id).all()
            }
            player_ids = [
                pid for (pid,) in s.query(Player.player_id).join(Player.groups)
                .filter(Group.group_id == group_id).all()
                if pid not in hidden
            ]

            conn = _rc()
            agg: dict[int, list[int]] = {}       # item_id -> [qty, value]
            loot: dict[int, int] = {}            # player_id -> total_loot
            recent_raw: list[tuple[int, dict]] = []  # (player_id, drop dict)
            if conn is not None and player_ids:
                # One pipeline pass: items hash + loot total + recent list per player.
                pipe = conn.pipeline()
                for pid in player_ids:
                    pipe.hgetall(_player_key(pid, token, "total_items"))
                    pipe.get(_player_key(pid, token, "total_loot"))
                    pipe.lrange(_player_key(pid, token, "recent_items"), 0, -1)
                results = pipe.execute()
                for i, pid in enumerate(player_ids):
                    items_res, loot_res, recent_res = results[3 * i : 3 * i + 3]
                    if items_res:
                        for item_raw, blob in items_res.items():
                            try:
                                item_id = int(_as_str(item_raw))
                            except Exception:
                                continue
                            qty, val = _parse_item_value(blob)
                            # Respect only_include_items_over_minimum (per-item unit value).
                            if only_over_min and qty > 0 and (val // qty) < min_value:
                                continue
                            row = agg.setdefault(item_id, [0, 0])
                            row[0] += qty
                            row[1] += val
                    if loot_res is not None:
                        try:
                            loot[pid] = int(float(_as_str(loot_res)))
                        except Exception:
                            pass
                    for raw in (recent_res or []):
                        try:
                            recent_raw.append((pid, json.loads(_as_str(raw))))
                        except Exception:
                            continue

            ranked = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_ITEMS]

            # --- Recent drops: high-value, newest first (generator.draw_recent_drops). ---
            recents = [
                (pid, d) for (pid, d) in recent_raw
                if isinstance(d, dict) and int(d.get("value") or 0) >= min_value
            ]
            # De-dup by drop_id (a drop can appear across granularities), keep newest.
            seen: set = set()
            recents.sort(key=lambda pd: str(pd[1].get("date_added", "")), reverse=True)
            deduped: list[tuple[int, dict]] = []
            for pid, d in recents:
                did = d.get("drop_id")
                if did is not None:
                    if did in seen:
                        continue
                    seen.add(did)
                deduped.append((pid, d))
            deduped = deduped[:MAX_RECENT]

            # --- Leaderboard: top players by total loot (generator.draw_leaderboard). ---
            top_players = sorted(loot.items(), key=lambda kv: kv[1], reverse=True)[:MAX_LEADERBOARD]

            # --- Name lookups (items + recents share ItemList; recents + board share Player). ---
            item_ids = {iid for iid, _ in ranked} | {int(d.get("item_id")) for _, d in deduped if d.get("item_id") is not None}
            item_names: dict[int, str] = {}
            if item_ids:
                item_names = {
                    iid: name for iid, name in
                    s.query(ItemList.item_id, ItemList.item_name)
                    .filter(ItemList.item_id.in_(item_ids)).all()
                }
            pids = {pid for pid, _ in deduped} | {pid for pid, _ in top_players}
            player_names: dict[int, str] = {}
            if pids:
                player_names = {
                    pid: name for pid, name in
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(pids)).all()
                }

            def _icon_url(iid: int, qty: int) -> str:
                icon_id = _coin_image_id(qty) if iid == 995 else iid
                return f"{IMG_BASE}/itemdb/{icon_id}.png"

            items = [
                {
                    "item_id": iid,
                    "name": item_names.get(iid, f"Item {iid}"),
                    "quantity": qv[0],
                    "value": money(qv[1]),
                    "icon_url": _icon_url(iid, qv[0]),
                    "is_coin": iid == 995,
                }
                for iid, qv in ranked
            ]

            recent_drops = []
            for pid, d in deduped:
                iid = int(d.get("item_id") or 0)
                qty = int(d.get("quantity") or 1)
                recent_drops.append({
                    "item_id": iid,
                    "name": item_names.get(iid, f"Item {iid}"),
                    "icon_url": _icon_url(iid, qty),
                    "player_id": pid,
                    "player_name": player_names.get(pid, "Unknown"),
                    "quantity": qty,
                    "value": money(int(d.get("value") or 0)),
                    "date_added": d.get("date_added"),
                })

            leaderboard = [
                {
                    "rank": idx + 1,
                    "player_id": pid,
                    "player_name": player_names.get(pid, "Unknown"),
                    "total": money(total),
                }
                for idx, (pid, total) in enumerate(top_players)
            ]

            total_value = sum(qv[1] for _, qv in agg.items())

            # Header prefix mirrors generator.draw_headers (group 2 == all players).
            date_label = _period_label(token)
            if group_id == 2:
                header = f"Tracked Drops - All Players ({date_label}) - "
            else:
                header = f"{group.group_name}'s Tracked Drops for {date_label} - "

            return {
                "group_id": group_id,
                "period": token,
                "total": money(total_value),
                "items": items,
                "background_url": background_url,
                "canvas": CANVAS,
                "header": header,
                "use_gp_colors": use_gp_colors,
                "use_dynamic_colors": False,
                "recent_drops": recent_drops,
                "leaderboard": leaderboard,
            }

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Group not found", f"No group with id {group_id}.")
    return with_cache_headers(jsonify(payload), max_age=30)


@lootboard_bp.post("/groups/<int:group_id>/lootboard/generate")
async def generate_lootboard_image(group_id: int):
    """Wrap the existing PNG generator (share affordance). Returns { url } or
    { url: null } if generation is unavailable."""
    body = None
    try:
        body = await request.get_json(silent=True)
    except Exception:
        body = None
    period = (body or {}).get("period", "all")

    payload = {"group_id": group_id}
    # The intake board generator accepts an optional timeframe; "all" => default.
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{INTAKE_API_URL}/generate-timeframe-board", json=payload
            )
        data = resp.json()
    except Exception:
        return jsonify({"url": None})

    url = None
    if isinstance(data, dict) and data.get("success"):
        url = data.get("image_url")
    return jsonify({"url": url})
