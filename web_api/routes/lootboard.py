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
from utils.dynamic_handling import get_stacked_display_id
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
# Per-item tooltip breakdown: top recipients by contributed value.
MAX_CONTRIBUTORS = 6
DEFAULT_MIN_VALUE = 2_500_000

# Filesystem prefix shared by every ``LootboardStyle.local_url`` row. The
# matching directory is exposed over the image server via the symlink
# ``static/assets/img/lootboard -> ../../lootboard`` (see lootboard route docs).
_THEME_PREFIX = "/store/droptracker/disc/lootboard/"
_DEFAULT_THEME_URL = f"{IMG_BASE}/lootboard/bank-new-clean-dark.png"

# Rendered preview images for the style catalog (one per ``lootboards`` row,
# named ``{id}.png``), served by the image host at /img/lootboards/{id}.png.
_STYLE_PREVIEW_DIR = "/store/droptracker/disc/static/assets/img/lootboards"


@lootboard_bp.get("/lootboard-styles")
async def lootboard_styles():
    """Catalog of selectable lootboard styles (public, cached).

    Backs the board-style picker in the group config editor — the ~87-row
    ``lootboards`` table the legacy XenForo selector used. Styles without a
    rendered preview image are omitted so the picker never shows a broken
    thumbnail.
    """
    def _load():
        with db_session() as s:
            rows = (
                s.query(LootboardStyle)
                .order_by(LootboardStyle.category, LootboardStyle.id)
                .all()
            )
            styles = []
            for r in rows:
                if not os.path.exists(os.path.join(_STYLE_PREVIEW_DIR, f"{r.id}.png")):
                    continue
                # Never offer a style whose theme background is missing on
                # disk — the PNG generator would fail for any group using it.
                if r.local_url and not os.path.exists(r.local_url):
                    continue
                styles.append({
                    "id": int(r.id),
                    "name": r.name or f"Style {r.id}",
                    "category": r.category or "Other",
                    "description": r.description or "",
                    "preview_url": f"{IMG_BASE}/lootboards/{r.id}.png",
                })
            return styles

    styles = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify({"styles": styles}), max_age=300)

# Item icon PNGs, served over the image server at ``{IMG_BASE}/itemdb/{id}.png``.
# Used to confirm a resolved stacked-pile icon is actually cached before pointing
# the browser at it (the web read path has no on-demand download, unlike the PNG
# generator's load_rl_cache_img).
_ITEMDB_DIR = "/store/droptracker/disc/static/assets/img/itemdb"

def _coin_image_id(quantity: int) -> int:
    """Coin pile sprite for ``quantity``.

    This used to be a third hand-written copy of the coin table, and carried the
    same wrong thresholds as the other two (10/50/100 rather than the game's
    25/100/250). It now defers to the shared helper, which reads them from the
    game cache.
    """
    from utils.dynamic_handling import get_coin_image_id

    return get_coin_image_id(quantity)


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


def _parse_item_blob(raw) -> tuple[int, int, str | None]:
    """Return (quantity, total_value, last_received) from a
    ``qty,value,count,first,last`` blob. ``last`` is the raw
    ``YYYY-MM-DD HH:MM:SS`` string (or None on old/malformed blobs) — it feeds
    the per-player "how long ago" line in the item-stack tooltips."""
    try:
        s = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        parts = s.split(",")
        qty, val = int(float(parts[0])), int(float(parts[1]))
        last = parts[4].strip() if len(parts) >= 5 and parts[4].strip() else None
        return qty, val, last
    except Exception:
        return 0, 0, None


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
            # item_id -> [(player_id, qty, value, last_received)] — feeds the
            # per-player breakdown in the item-stack tooltips.
            contributors: dict[int, list[tuple[int, int, int, str | None]]] = {}
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
                            qty, val, last = _parse_item_blob(blob)
                            # Respect only_include_items_over_minimum (per-item unit value).
                            if only_over_min and qty > 0 and (val // qty) < min_value:
                                continue
                            row = agg.setdefault(item_id, [0, 0])
                            row[0] += qty
                            row[1] += val
                            if qty > 0 or val > 0:
                                contributors.setdefault(item_id, []).append((pid, qty, val, last))
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

            # Top recipients per ranked item (by contributed value). Capped so a
            # stackable farmed by the whole clan doesn't bloat the payload; the
            # tooltip shows the rest as "+N more players".
            top_contribs: dict[int, list[tuple[int, int, int, str | None]]] = {}
            for iid, _qv in ranked:
                rows = sorted(contributors.get(iid, []), key=lambda r: r[2], reverse=True)
                top_contribs[iid] = rows[:MAX_CONTRIBUTORS]

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
            pids |= {row[0] for rows in top_contribs.values() for row in rows}
            player_names: dict[int, str] = {}
            if pids:
                player_names = {
                    pid: name for pid, name in
                    s.query(Player.player_id, Player.player_name)
                    .filter(Player.player_id.in_(pids)).all()
                }

            # Stackable items (Zulrah's scales, arrows, bolts, seeds, …) are
            # stored as their single-unit id but render best as their largest-
            # stack pile icon (suggestion #44). Coins stay magnitude-based
            # (per-quantity). Stack resolution is quantity-independent — memoise it.
            _stacked_icon_cache: dict[int, int] = {}

            def _resolve_icon_id(iid: int, qty: int) -> int:
                if iid == 995:
                    return _coin_image_id(qty)
                if iid not in _stacked_icon_cache:
                    resolved = get_stacked_display_id(iid, s)
                    # Keep the submitted id if the pile variant isn't cached yet,
                    # so the browser never points at a missing icon.
                    if resolved != iid and not os.path.exists(f"{_ITEMDB_DIR}/{resolved}.png"):
                        resolved = iid
                    _stacked_icon_cache[iid] = resolved
                return _stacked_icon_cache[iid]

            def _icon_url(iid: int, qty: int) -> str:
                return f"{IMG_BASE}/itemdb/{_resolve_icon_id(iid, qty)}.png"

            items = []
            for iid, qv in ranked:
                item = {
                    "item_id": iid,
                    "name": item_names.get(iid, f"Item {iid}"),
                    "quantity": qv[0],
                    "value": money(qv[1]),
                    "icon_url": _icon_url(iid, qv[0]),
                    "is_coin": iid == 995,
                }
                rows = top_contribs.get(iid, [])
                if rows:
                    item["contributors"] = [
                        {
                            "player_id": pid,
                            "player_name": player_names.get(pid, "Unknown"),
                            "quantity": q,
                            "value": money(v),
                            "last_at": last,
                        }
                        for pid, q, v, last in rows
                    ]
                    item["contributor_count"] = len(contributors.get(iid, []))
                items.append(item)

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


@lootboard_bp.post("/groups/<int:group_id>/lootboard/timeframe")
async def generate_timeframe_lootboard(group_id: int):
    """Generate a custom-timeframe lootboard PNG for group leaders.

    Body: { "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD" } (inclusive).
    Group-admin gated. Data comes from the tiered timeframe sources
    (lootboard/timeframe.py): Redis daily hashes for recent ranges, the
    player_item_hourly_totals rollup for older ones. The PNG render runs in a
    subprocess (CPU-bound PIL work must not block the API workers), guarded by
    a per-group Redis cooldown so a click-happy admin can't stack renders.
    """
    import subprocess  # noqa: F401  (documentational; asyncio subprocess below)
    import sys
    from datetime import date as _date, datetime as _dt

    from lootboard import timeframe as tf
    from web_api.deps import (
        assert_group_admin,
        current_user_id,
        json_body,
        load_user,
        manageable_guild_ids,
    )

    user_id = current_user_id()
    body = await json_body()

    def _parse_day(key: str) -> _date:
        raw = str((body or {}).get(key, "")).strip()
        try:
            return _dt.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            abort_problem(422, "Invalid date", f"'{key}' must be YYYY-MM-DD.")

    start_day = _parse_day("start_date")
    end_day = _parse_day("end_date")

    # Range sanity + tier selection (raises with a user-presentable message).
    try:
        plan = tf.classify_range(start_day, end_day)
    except ValueError as e:
        abort_problem(422, "Invalid range", str(e))

    def _authorize_and_precheck():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            if plan.mode == "hourly":
                missing = tf.missing_rollup_partitions(
                    s, tf.month_partitions(start_day, end_day)
                )
                if missing:
                    abort_problem(
                        422, "Range not available yet",
                        "Loot data for "
                        + ", ".join(f"{p // 100}-{p % 100:02d}" for p in missing)
                        + " is still being backfilled — try a more recent range.",
                    )

    await asyncio.to_thread(_authorize_and_precheck)

    # Per-group cooldown (multi-worker safe via Redis SET NX). The abort must
    # live OUTSIDE the try: it raises, and a blanket except would swallow it.
    conn = _rc()
    on_cooldown = False
    if conn is not None:
        try:
            on_cooldown = not conn.set(f"tfboard:cooldown:{group_id}", "1", nx=True, ex=60)
        except Exception:
            on_cooldown = False  # cooldown is best-effort; never block on Redis
    if on_cooldown:
        abort_problem(429, "Slow down",
                      "A board was just generated for this group — try again in a minute.")

    start_iso = f"{start_day.isoformat()}T00:00:00"
    end_iso = f"{end_day.isoformat()}T23:59:59"
    cmd = [
        sys.executable, "timeframe_board_cli.py",
        "--group-id", str(group_id),
        "--start-time", start_iso,
        "--end-time", end_iso,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd="/store/droptracker/disc",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        abort_problem(504, "Generation timed out", "Board generation took longer than 120s.")

    stdout_str = (stdout or b"").decode(errors="ignore").strip()
    stderr_str = (stderr or b"").decode(errors="ignore").strip()
    if proc.returncode != 0:
        # The CLI surfaces classify/backfill errors as "Error generating board: <msg>".
        detail = stderr_str.splitlines()[-1] if stderr_str else "Board generation failed."
        if detail.startswith("Error generating board: "):
            detail = detail[len("Error generating board: "):]
        abort_problem(422, "Generation failed", detail)

    image_path = next(
        (line.strip() for line in reversed(stdout_str.splitlines())
         if line.strip().startswith("/store/") and line.strip().endswith(".png")),
        None,
    )
    url = tf.image_path_to_url(image_path or "")
    if not url:
        abort_problem(500, "Generation failed", "Generator returned no image path.")
    return jsonify({
        "url": url,
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "source": plan.mode,
    })


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
