"""Task 13 — native lootboard data.

  GET  /api/v1/groups/{id}/lootboard?period=          (public, cached) -> Lootboard
  POST /api/v1/groups/{id}/lootboard/generate          -> { url }  (wraps the PNG CLI)

The JSON board is aggregated from the same per-player Redis item hashes that
feed the legacy PNG generator (``player:{id}:{token}:total_items`` — value
``"qty,value,count,first,last"`` per item), so no image/PIL dependency is
pulled into the read path. Hidden (ignored) players are excluded.
"""
from __future__ import annotations

import asyncio
import os

import httpx
from quart import Blueprint, jsonify, request

from db import Group, IgnoredPlayer, ItemList, Player
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
MAX_ITEMS = 100


def _item_hash_key(player_id: int, token: str) -> str:
    """Per-player item-totals hash key for a partition token.

    Matches ``services/redis_updates`` key layout. Weekly has no per-item hash,
    so it falls back to the monthly hash (documented limitation)."""
    if token == "all":
        return f"player:{player_id}:all:total_items"
    if len(token) == 8 and token.isdigit():  # YYYYMMDD
        return f"player:{player_id}:daily:{token}:total_items"
    if "W" in token:  # weekly -> fall back to current month
        from utils.partitions import month_token

        return f"player:{player_id}:{month_token()}:total_items"
    return f"player:{player_id}:{token}:total_items"  # YYYYMM


def _parse_item_value(raw) -> tuple[int, int]:
    """Return (quantity, total_value) from a ``qty,value,count,first,last`` blob."""
    try:
        s = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        parts = s.split(",")
        return int(float(parts[0])), int(float(parts[1]))
    except Exception:
        return 0, 0


@lootboard_bp.get("/groups/<int:group_id>/lootboard")
async def group_lootboard(group_id: int):
    period = request.args.get("period", "all")
    token = resolve_period(period)

    def _load():
        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                return None

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
            agg: dict[int, list[int]] = {}  # item_id -> [qty, value]
            if conn is not None and player_ids:
                pipe = conn.pipeline()
                for pid in player_ids:
                    pipe.hgetall(_item_hash_key(pid, token))
                for result in pipe.execute():
                    if not result:
                        continue
                    for item_raw, blob in result.items():
                        try:
                            item_id = int(
                                item_raw.decode("utf-8")
                                if isinstance(item_raw, (bytes, bytearray))
                                else item_raw
                            )
                        except Exception:
                            continue
                        qty, val = _parse_item_value(blob)
                        row = agg.setdefault(item_id, [0, 0])
                        row[0] += qty
                        row[1] += val

            ranked = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)[:MAX_ITEMS]
            item_ids = [iid for iid, _ in ranked]
            names = {}
            if item_ids:
                rows = (
                    s.query(ItemList.item_id, ItemList.item_name)
                    .filter(ItemList.item_id.in_(item_ids))
                    .all()
                )
                names = {iid: name for iid, name in rows}

            items = [
                {
                    "item_id": iid,
                    "name": names.get(iid, f"Item {iid}"),
                    "quantity": qv[0],
                    "value": money(qv[1]),
                    "icon_url": f"{IMG_BASE}/itemdb/{iid}.png",
                }
                for iid, qv in ranked
            ]
            total_value = sum(qv[1] for _, qv in agg.items())

            return {
                "group_id": group_id,
                "period": token,
                "total": money(total_value),
                "items": items,
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
