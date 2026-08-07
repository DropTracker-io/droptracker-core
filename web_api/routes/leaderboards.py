"""Task 04 — leaderboards.

GET /api/v1/leaderboards/players?period=&scope=&page=&limit=
GET /api/v1/leaderboards/groups?period=&page=&limit=

Players read the canonical Redis sorted set directly (paged ZREVRANGE). Groups
currently recompute per-partition totals across groups (cached), matching the
legacy `/top_groups` behavior; Task 07 Part B replaces this with a precomputed
per-partition group sorted set.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request

from db import Player, Group
from web_api.flair import group_flairs
from web_api.common import (
    cache_get,
    cache_set,
    db_session,
    decode_member,
    group_totals_key,
    hidden_player_ids,
    leaderboard_key,
    money,
    npc_leaderboard_key,
    parse_page,
    player_list_loot_sum,
    resolve_period,
    with_cache_headers,
    _rc,
)

leaderboards_bp = Blueprint("v1_leaderboards", __name__)

GROUP_TOTALS_TTL = 30.0
BADGES_TTL = 30.0
# Chips sent per row; the frontend shows the first few and a "+N" overflow.
_MAX_ROW_BADGES = 6
IMG_BASE = "https://www.droptracker.io/img"


def _ctx_recency(context) -> str:
    """Sortable "which award is newer" token from an award's context.

    Day-scoped badges carry ``day`` ("20260704"), the loot leaders carry the
    partition token ``period`` ("202607"). Both are zero-padded, and only
    awards of the *same* badge key are ever compared, so a plain string
    compare orders them correctly.
    """
    ctx = context if isinstance(context, dict) else {}
    return str(ctx.get("day") or ctx.get("period") or "")


def _badge_priority(semantic: str, key: str) -> int:
    # Global loot leaders first — there is exactly one per board, and a row is
    # capped at _MAX_ROW_BADGES, so a record-heavy #1 would otherwise push its
    # own crown into the overflow. Then held records, streaks, and repeatable
    # permanents (daily champion).
    if key.startswith("global_loot_leader"):
        return 0
    if semantic == "held":
        return 1
    if key.startswith("loot_streak"):
        return 2
    return 3


def _compact_badges_for(ids: list[int]) -> dict[int, list[dict]]:
    """One indexed query for a page of player ids -> compact badge chips.

    Global badges only (group_key=0). Each chip carries the award's context so
    the UI can render specifics ("Daily Loot Champion (Jul 3, 2026)", "Boss
    Record (Zulrah, Solo)"). Held awards get one chip per record they hold;
    repeatable permanents (e.g. many daily-champion days) collapse into one
    chip with a count and the most recent context. Cached in-process ~30s per
    id-set; this endpoint is hot, so any failure here is swallowed by the
    caller and the field is simply omitted.
    """
    import json as _json

    cache_key = "lb:badges:" + ",".join(map(str, sorted(ids)))
    cached = cache_get(cache_key, BADGES_TTL)
    if cached is not None:
        return cached

    from db import Badge, PlayerBadge

    with db_session() as s:
        rows = (
            s.query(
                PlayerBadge.player_id,
                PlayerBadge.context,
                Badge.key,
                Badge.name,
                Badge.tone,
                Badge.icon_emoji,
                Badge.icon_url,
                Badge.semantic,
            )
            .join(Badge, Badge.badge_id == PlayerBadge.badge_id)
            .filter(
                PlayerBadge.player_id.in_(ids),
                PlayerBadge.status == "active",
                PlayerBadge.group_key == 0,
                Badge.active == True,  # noqa: E712
            )
            .order_by(PlayerBadge.awarded_at.desc())
            .all()
        )

    out: dict[int, list[dict]] = {}
    for pid, ctx_raw, key, name, tone, emoji, icon_url, semantic in rows:
        context = None
        if ctx_raw:
            try:
                context = _json.loads(ctx_raw)
            except (TypeError, ValueError):
                context = None
        # NPC-scoped awards use the NPC's icon when the badge has no custom one.
        if not icon_url and isinstance(context, dict) and context.get("npc_id"):
            icon_url = f"{IMG_BASE}/npcdb/{context['npc_id']}.png"

        chips = out.setdefault(int(pid), [])
        if semantic != "held":
            # Collapse repeats of the same permanent badge into one chip,
            # keeping the most recent context. Backfilled awards can share an
            # awarded_at second, so compare the context's own period/day token
            # rather than trusting row order.
            for c in chips:
                if c["key"] == key:
                    c["count"] = c.get("count", 1) + 1
                    if _ctx_recency(context) > _ctx_recency(c.get("context")):
                        c["context"] = context
                    break
            else:
                chips.append({
                    "key": key, "label": name, "tone": tone, "emoji": emoji,
                    "icon_url": icon_url, "count": 1, "context": context,
                    "_prio": _badge_priority(semantic, key),
                })
        else:
            chips.append({
                "key": key, "label": name, "tone": tone, "emoji": emoji,
                "icon_url": icon_url, "count": 1, "context": context,
                "_prio": _badge_priority(semantic, key),
            })

    for pid, chips in out.items():
        chips.sort(key=lambda c: (c["_prio"], c["key"]))
        out[pid] = [
            {k: v for k, v in c.items() if k != "_prio"}
            for c in chips[:_MAX_ROW_BADGES]
        ]
    cache_set(cache_key, out)
    return out


@leaderboards_bp.get("/leaderboards/players")
async def leaderboards_players():
    # Default to the current month — the tracking system works month-to-month and
    # the all-time board is a secondary view. The "month" sentinel is resolved to
    # the current-month partition by resolve_period's fallback.
    period = request.args.get("period", "month")
    scope = request.args.get("scope", "global")
    page, limit = parse_page(request)
    token = resolve_period(period)

    group_id = None
    npc_id = None
    if scope.startswith("group:"):
        # Plain "group:{gid}" or the combined "group:{gid}:npc:{nid}" form
        # (per-group per-NPC boards; the Redis keys have always existed —
        # npc_leaderboard_key(token, npc_id, group_id=...) — only this scope
        # grammar was missing).
        try:
            parts = scope.split(":")
            group_id = int(parts[1])
            if len(parts) >= 4 and parts[2] == "npc":
                npc_id = int(parts[3])
        except Exception:
            group_id = None
            npc_id = None
    elif scope.startswith("npc:"):
        try:
            npc_id = int(scope.split(":", 1)[1])
        except Exception:
            npc_id = None

    if npc_id is not None:
        key = npc_leaderboard_key(token, npc_id, group_id=group_id)
    else:
        key = leaderboard_key(token, group_id=group_id)
    conn = _rc()

    entries = []
    total = 0
    if conn is not None:
        def _read():
            start = (page - 1) * limit
            end = start + limit - 1
            raw = conn.zrevrange(key, start, end, withscores=True)
            card = conn.zcard(key)
            return raw, card

        raw, total = await asyncio.to_thread(_read)

        # Privacy: drop hidden players from the page (user "Hidden" setting or
        # per-account hide). Ranks keep their Redis positions, so a filtered
        # row leaves a gap rather than reshuffling everyone below it.
        hidden = await asyncio.to_thread(hidden_player_ids)

        start_rank = (page - 1) * limit
        ids = []
        scored = []
        for pos, (member_raw, score) in enumerate(raw):
            pid = decode_member(member_raw)
            if pid is None or pid in hidden:
                continue
            ids.append(pid)
            scored.append((start_rank + pos + 1, pid, int(float(score))))

        name_map = {}
        if ids:
            with db_session() as s:
                rows = s.query(Player.player_id, Player.player_name).filter(Player.player_id.in_(ids)).all()
                name_map = {pid: name for pid, name in rows}

        # Best-effort compact badges; omit the field entirely on any failure.
        badge_map: dict[int, list[dict]] = {}
        if ids:
            try:
                badge_map = await asyncio.to_thread(_compact_badges_for, ids)
            except Exception:
                badge_map = {}

        for rank, pid, loot in scored:
            row = {
                "rank": rank,
                "id": pid,
                "name": name_map.get(pid, f"Player {pid}"),
                "loot": money(loot),
            }
            chips = badge_map.get(pid)
            if chips:
                row["badges"] = chips
            entries.append(row)

    resp = jsonify({
        "period": token,
        "scope": scope,
        "entries": entries,
        "meta": {"page": page, "limit": limit, "total": int(total)},
    })
    return with_cache_headers(resp, max_age=15)


def _read_group_totals_precomputed(token):
    """Read the precomputed group-total sorted set (`gleaderboard:{token}`,
    maintained by the lootboard generator). Returns a sorted desc list of
    (group_id, score) or None if the set is empty/absent."""
    conn = _rc()
    if conn is None:
        return None
    try:
        raw = conn.zrevrange(group_totals_key(token), 0, -1, withscores=True)
    except Exception:
        return None
    if not raw:
        return None
    out = []
    for member_raw, score in raw:
        gid = decode_member(member_raw)
        if gid is None or gid in (0, 2):
            continue
        out.append((gid, int(float(score))))
    return out or None


def _compute_group_totals(token):
    """Sorted [(group_id, name, total, members)] desc across all non-system groups.

    ``token`` is a resolved partition token (``YYYYMM`` | ``YYYYWww`` |
    ``YYYYMMDD`` | ``all``). Each group's total is the sum of its members'
    per-token loot, read from the maintained player boards
    (``player:{id}:{token}:total_loot`` → ``leaderboard:{token}`` fallback). This
    is what makes the group all-time/weekly/daily boards correct: they derive
    from the same per-player period totals used for the player leaderboards,
    rather than always summing the current month.

    Cached per-token (in-process). O(all groups) — a precomputed per-token group
    sorted set (Task 07 Part B) can replace this later.
    """
    cache_key = f"group_totals:{token}"
    cached = cache_get(cache_key, GROUP_TOTALS_TTL)
    if cached is not None:
        return cached

    result = []
    with db_session() as s:
        groups = s.query(Group).all()
        for g in groups:
            if g.group_id in (0, 2):  # 0/2 are reserved/global
                continue
            player_ids = [
                pid for (pid,) in s.query(Player.player_id).join(Player.groups)
                .filter(Group.group_id == g.group_id).all()
            ]
            total = player_list_loot_sum(player_ids, token)
            result.append((g.group_id, g.group_name, total, len(player_ids)))

    result.sort(key=lambda x: x[2], reverse=True)
    cache_set(cache_key, result)
    return result


@leaderboards_bp.get("/leaderboards/groups")
async def leaderboards_groups():
    # Default to the current month (see `leaderboards_players`).
    period = request.args.get("period", "month")
    page, limit = parse_page(request)
    token = resolve_period(period)

    def _load():
        # Prefer the precomputed group-total set (O(page), no full recompute).
        precomputed = _read_group_totals_precomputed(token)
        if precomputed is not None:
            total_count = len(precomputed)
            start = (page - 1) * limit
            window = precomputed[start:start + limit]
            gids = [gid for gid, _ in window]
            names = {}
            flairs = {}
            if gids:
                with db_session() as s:
                    rows = (
                        s.query(Group.group_id, Group.group_name)
                        .filter(Group.group_id.in_(gids))
                        .all()
                    )
                    names = {gid: name for gid, name in rows}
                    flairs = group_flairs(s, gids)
            entries = []
            for i, (gid, total) in enumerate(window):
                row = {
                    "rank": start + i + 1,
                    "id": gid,
                    "name": names.get(gid, f"Group {gid}"),
                    "loot": money(total),
                }
                flair = flairs.get(gid)
                if flair:
                    row["flair"] = flair
                entries.append(row)
            return entries, total_count

        # Fallback: compute across members (cached). Used for periods the
        # precompute doesn't cover (all-time / weekly / daily), aggregating each
        # group's members over the resolved token.
        totals = _compute_group_totals(token)
        start = (page - 1) * limit
        window = totals[start:start + limit]
        gids = [gid for (gid, _name, _total, _members) in window]
        flairs = {}
        if gids:
            with db_session() as s:
                flairs = group_flairs(s, gids)
        entries = []
        for i, (gid, name, total, _members) in enumerate(window):
            row = {
                "rank": start + i + 1,
                "id": gid,
                "name": name,
                "loot": money(total),
            }
            flair = flairs.get(gid)
            if flair:
                row["flair"] = flair
            entries.append(row)
        return entries, len(totals)

    entries, total_count = await asyncio.to_thread(_load)

    resp = jsonify({
        "period": token,
        "scope": "groups",
        "entries": entries,
        "meta": {"page": page, "limit": limit, "total": total_count},
    })
    return with_cache_headers(resp, max_age=15)
