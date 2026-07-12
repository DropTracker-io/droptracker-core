"""Realtime event publishing over Redis pub/sub (FRONTEND_PLAN.md §8, Task 07 C).

Additive: publishes happen at the exact points work already occurs (a drop is
credited, an announcement is published). They never change processing semantics
and never raise into the caller.

Channels are ``rt:{scope}`` where scope ∈ ``global`` | ``group:{id}`` |
``player:{id}`` | ``npc:{id}``. The event envelope matches the web client's
``RealtimeEventSchema`` (§8.3):

    { "v": 1, "type": "leaderboard_delta", "scope": "group:42",
      "ts": 1719000000, "data": { ...display-ready... } }

``data`` is already formatted for display; the browser renders it directly.
"""
from __future__ import annotations

import json
import time
from typing import Iterable, Optional

from utils.redis import redis_client

CHANNEL_PREFIX = "rt:"


def _rc():
    return getattr(redis_client, "client", None)


def publish_event(event_type: str, scope: str, data: dict) -> None:
    """Publish one event to ``rt:{scope}``. Best-effort; never raises."""
    conn = _rc()
    if conn is None:
        return
    envelope = {
        "v": 1,
        "type": event_type,
        "scope": scope,
        "ts": int(time.time()),
        "data": data,
    }
    try:
        conn.publish(f"{CHANNEL_PREFIX}{scope}", json.dumps(envelope))
    except Exception:
        pass


def publish_to_scopes(event_type: str, scopes: Iterable[str], data: dict) -> None:
    """Publish the same event to several scopes (each frame carries its scope)."""
    for scope in scopes:
        publish_event(event_type, scope, data)


def publish_event_update(event_id: int, data: dict) -> None:
    """Publish an event-engine update (Task 17) to ``rt:event:{id}``.

    ``data.kind`` ∈ progress|completion|cell|line|blackout|pending|revoke,
    plus task_id/team_id and optional cell_idx/cell_label/points/bonus_points/
    team_score/player_name — display-ready for the live event page.
    Best-effort; never raises.
    """
    publish_event("event_update", f"event:{int(event_id)}", data)


IMG_BASE = "https://www.droptracker.io/img"

FEED_HISTORY_KEY = "feed:recent"
FEED_HISTORY_MAX = 30


def publish_drop(player, drop, total_value: int, partition: int,
                 group_ids: Optional[list] = None, world_type: str = "main",
                 item_name: Optional[str] = None, npc_name: Optional[str] = None) -> None:
    """Publish a ``leaderboard_delta`` for a credited drop to every relevant
    scope (global, the player's groups, the player, the npc), plus a ``drop``
    event to the site-wide live feed (§UI ticker). Main world only — seasonal
    drops don't feed the public live leaderboards.

    ``item_name``/``npc_name`` are optional, already-resolved display strings
    from the caller (never queried here) so this stays a zero-extra-query,
    best-effort publish off the intake hot path.
    """
    if world_type != "main":
        return
    conn = _rc()
    if conn is None:
        return

    try:
        from utils.format import format_number

        player_id = player.player_id
        new_total = None
        rank = None
        try:
            score = conn.zscore(f"leaderboard:{partition}", player_id)
            if score is not None:
                new_total = int(float(score))
            r = conn.zrevrank(f"leaderboard:{partition}", player_id)
            if r is not None:
                rank = int(r) + 1
        except Exception:
            pass

        data = {
            "id": player_id,
            "name": getattr(player, "player_name", None),
            "delta": int(total_value),
        }
        if rank is not None:
            data["rank"] = rank
        if new_total is not None:
            data["total_loot_formatted"] = format_number(new_total)

        scopes = ["global", f"player:{player_id}"]
        for gid in (group_ids or []):
            scopes.append(f"group:{gid}")
        npc_id = getattr(drop, "npc_id", None)
        if npc_id:
            scopes.append(f"npc:{npc_id}")

        publish_to_scopes("leaderboard_delta", scopes, data)

        # Site-wide live drop feed (header ticker). Only fires for drops with a
        # value worth surfacing, to keep the ticker meaningful under load.
        if total_value >= 1_000_000:
            item_id = getattr(drop, "item_id", None)
            feed_data = {
                "ts": int(time.time()),
                "player_id": player_id,
                "player_name": getattr(player, "player_name", None),
                "item_id": item_id,
                "item_name": item_name,
                "npc_id": npc_id,
                "npc_name": npc_name,
                "value": int(total_value),
                "value_formatted": format_number(int(total_value)),
            }
            if item_id:
                feed_data["icon_url"] = f"{IMG_BASE}/itemdb/{item_id}.png"
            if npc_id:
                feed_data["npc_icon_url"] = f"{IMG_BASE}/npcdb/{npc_id}.png"
            publish_event("drop", "feed", feed_data)

            # Persist to a capped history list so the ticker can hydrate with
            # past drops on page load instead of starting empty.
            try:
                conn.lpush(FEED_HISTORY_KEY, json.dumps(feed_data))
                conn.ltrim(FEED_HISTORY_KEY, 0, FEED_HISTORY_MAX - 1)
            except Exception:
                pass
    except Exception:
        pass
