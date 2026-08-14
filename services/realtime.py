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


def publish_chat_message(thread_id: int, payload: dict,
                         audience: Optional[Iterable[int]] = None) -> None:
    """Publish a chat entry to ``rt:chat:{thread_id}`` (web96a).

    Two frames with deliberately different weights:

    * ``chat_message`` on ``rt:chat:{id}`` carries the full entry. That scope is
      membership-gated when the client subscribes (``web_api/routes/realtime``),
      so it is the only place message content travels.
    * ``chat_unread`` on ``rt:user:{uid}`` carries only the thread id — enough
      to light a badge for someone who is not on the page. It goes to a wider
      audience (every admin of every participating clan), so it must never
      include the body.

    Best-effort; never raises.
    """
    publish_event("chat_message", f"chat:{int(thread_id)}", payload)
    hint = {"thread_id": int(thread_id), "message_id": payload.get("id")}
    for user_id in (audience or []):
        publish_event("chat_unread", f"user:{int(user_id)}", hint)


IMG_BASE = "https://www.droptracker.io/img"

FEED_HISTORY_KEY = "feed:recent"
FEED_HISTORY_MAX = 40

# Site-wide ticker gates. Drops must be a single high-value item (one Twisted
# bow, not 40k cannonballs) so the banner stays a highlight reel, not a log.
FEED_MIN_DROP_VALUE = 10_000_000
# "New player started tracking" fires constantly at intake volume — sample it
# down to at most one ticker entry per cooldown window.
FEED_NEW_PLAYER_COOLDOWN_SECONDS = 3600
_FEED_NEW_PLAYER_COOLDOWN_KEY = "feed:new_player:cooldown"


def publish_feed_event(event_type: str, data: dict) -> None:
    """Publish one event to the site-wide ticker (``rt:feed``) and persist the
    full envelope to the capped ``feed:recent`` history list so the ticker can
    hydrate typed entries on page load. Best-effort; never raises.

    ``data`` must be display-ready (names/urls resolved by the caller) — this
    module never touches the database.
    """
    conn = _rc()
    if conn is None:
        return
    try:
        data.setdefault("ts", int(time.time()))
        envelope = {
            "v": 1,
            "type": event_type,
            "scope": "feed",
            "ts": int(data["ts"]),
            "data": data,
        }
        payload = json.dumps(envelope)
        conn.publish(f"{CHANNEL_PREFIX}feed", payload)
        conn.lpush(FEED_HISTORY_KEY, payload)
        conn.ltrim(FEED_HISTORY_KEY, 0, FEED_HISTORY_MAX - 1)
    except Exception:
        pass


def publish_feed_personal_best(player_id: int, player_name: str, npc_id: int,
                               npc_name: str, time_ms: int, time_display: str,
                               team_size: str, rank: int) -> None:
    """Ticker entry for a new personal best that lands in the top of its
    (boss, team-size) leaderboard. Caller has already applied the rank gate."""
    publish_feed_event("personal_best", {
        "player_id": int(player_id),
        "player_name": player_name,
        "npc_id": int(npc_id),
        "npc_name": npc_name,
        "npc_icon_url": f"{IMG_BASE}/npcdb/{int(npc_id)}.png",
        "time_ms": int(time_ms),
        "time_display": time_display,
        "team_size": team_size,
        "rank": int(rank),
    })


def publish_feed_pet(player_id: int, player_name: str, pet_name: str,
                     item_id: Optional[int] = None,
                     npc_name: Optional[str] = None) -> None:
    """Ticker entry for a newly obtained pet."""
    data = {
        "player_id": int(player_id),
        "player_name": player_name,
        "pet_name": pet_name,
    }
    if item_id:
        data["item_id"] = int(item_id)
        data["icon_url"] = f"{IMG_BASE}/itemdb/{int(item_id)}.png"
    if npc_name:
        data["npc_name"] = npc_name
    publish_feed_event("pet", data)


def publish_feed_group_created(group_id: int, group_name: str) -> None:
    """Ticker entry for a newly registered group (web wizard or /create-group)."""
    publish_feed_event("group_created", {
        "group_id": int(group_id),
        "group_name": group_name,
    })


def feed_new_player_gate() -> bool:
    """True when a "new player" ticker entry may fire (claims the cooldown).

    Uses SET NX EX so concurrent intake processes race safely; callers only
    run their COUNT query after winning the slot.
    """
    conn = _rc()
    if conn is None:
        return False
    try:
        return bool(conn.set(
            _FEED_NEW_PLAYER_COOLDOWN_KEY, "1",
            nx=True, ex=FEED_NEW_PLAYER_COOLDOWN_SECONDS,
        ))
    except Exception:
        return False


def publish_feed_new_player(player_id: int, player_name: str,
                            player_number: Optional[int] = None) -> None:
    """Ticker entry for a newly tracked player (sampled via the gate above)."""
    data = {
        "player_id": int(player_id),
        "player_name": player_name,
    }
    if player_number:
        data["player_number"] = int(player_number)
    publish_feed_event("new_player", data)


def publish_feed_subscription(kind: str, name: str,
                              group_id: Optional[int] = None,
                              player_id: Optional[int] = None,
                              tier_key: Optional[str] = None) -> None:
    """Ticker entry for a NEW premium subscription (first settled payment only
    — the caller already filters renewals). ``kind`` ∈ group|user. No amounts:
    the public ticker celebrates the support, not the invoice.
    """
    data = {
        "kind": "group" if kind == "group" else "user",
        "name": name,
    }
    if group_id:
        data["group_id"] = int(group_id)
    if player_id:
        data["player_id"] = int(player_id)
    if tier_key:
        data["tier_key"] = tier_key
    publish_feed_event("subscription", data)


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

        # Site-wide live drop feed (header ticker). Single high-value items
        # only (>= 10M and quantity 1) so the ticker stays a highlight reel —
        # lower-value and stacked drops still get their leaderboard_delta above.
        quantity = getattr(drop, "quantity", 1) or 1
        if total_value >= FEED_MIN_DROP_VALUE and int(quantity) == 1:
            item_id = getattr(drop, "item_id", None)
            feed_data = {
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
            # Publishes rt:feed AND persists to the capped hydration history.
            publish_feed_event("drop", feed_data)
    except Exception:
        pass
