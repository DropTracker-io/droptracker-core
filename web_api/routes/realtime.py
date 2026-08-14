"""Task 07 Part D — SSE gateway.

    GET /api/v1/stream?channels=global,group:42,player:1337   (text/event-stream)

A lightweight async consumer SUBSCRIBEs to the requested ``rt:{scope}`` Redis
channels (published by ``services/realtime.py``) and relays matching events to
the client as SSE ``data:`` frames, with periodic heartbeats.

The BFF proxies this at ``/api/stream`` and forwards the session cookie. Public
scopes (global/group/npc) are anonymous; ``player:{id}`` requires a session.
"""
from __future__ import annotations

import asyncio
import json
import os

from quart import Blueprint, Response, jsonify, request

from web_api.common import group_ignored_player_ids, hidden_player_ids
from web_api.deps import optional_user_id

realtime_bp = Blueprint("v1_realtime", __name__)

_CHANNEL_PREFIX = "rt:"
_GROUP_SCOPE_PREFIX = "group:"


def _frame_scope(channel) -> str:
    """The scope a frame arrived on: ``rt:group:42`` -> ``group:42``.

    Taken from the Redis channel rather than the envelope's own ``scope`` field
    because the channel is what actually decides who receives the frame — it is
    the authoritative input to a privacy filter.
    """
    if isinstance(channel, (bytes, bytearray)):
        channel = channel.decode("utf-8", "ignore")
    channel = channel or ""
    if channel.startswith(_CHANNEL_PREFIX):
        return channel[len(_CHANNEL_PREFIX):]
    return channel


def _scope_group_id(scope: str):
    """The group id a ``group:{id}`` scope names, else None."""
    if not scope or not scope.startswith(_GROUP_SCOPE_PREFIX):
        return None
    try:
        return int(scope[len(_GROUP_SCOPE_PREFIX):])
    except (TypeError, ValueError):
        return None


async def _is_hidden_event(frame: str, scope: str = "") -> bool:
    """True when an rt:* frame names a player the scope's viewers must not see.

    Two independent hiding layers, and they have different reach:

    * **Global** (``players.hidden`` / ``users.hidden``, via
      ``hidden_player_ids``) — the player opted out of every public surface, so
      this applies on every scope.
    * **Per-group** (an ``ignored_players`` row, written when a group's leaders
      hide a member from the admin member listing) — applies ONLY to
      ``group:{id}`` frames, and only for the group that did the hiding. The
      hide is group-scoped: it must not follow the player onto the global/feed
      scopes, their own ``player:`` feed, or another group's feed. The
      lootboards and (2026-08-14) the Discord notifications already honour it;
      this stream was the last surface still showing hidden members.

    Best-effort: unparseable or player-less frames pass through.
    """
    try:
        envelope = json.loads(frame)
        # Only these carry a player id in `data` ("id"/"player_id" mean other
        # entities on announcement/event_update frames).
        if envelope.get("type") not in (
            "drop", "leaderboard_delta",
            # Feed-ticker types that name a player (services/realtime.py).
            "personal_best", "pet", "new_player", "subscription",
        ):
            return False
        data = envelope.get("data") or {}
        pid = data.get("player_id", data.get("id"))
        if not isinstance(pid, int):
            return False
        hidden = await asyncio.to_thread(hidden_player_ids)
        if pid in hidden:
            return True
        group_id = _scope_group_id(scope)
        if group_id is None:
            return False
        # Both lookups cache ~60s in-process, so this stays off the DB on the
        # per-frame hot path.
        ignored = await asyncio.to_thread(group_ignored_player_ids, group_id)
        return pid in ignored
    except Exception:
        return False

_HEARTBEAT_SECONDS = 20
_MAX_CHANNELS = 24
_PUBLIC_EXACT_SCOPES = ("global", "feed")  # "feed" = site-wide live drop ticker
_FEED_HISTORY_KEY = "feed:recent"


# Whether an event is private changes about never, and the answer is the same
# for every viewer — so it is cached briefly to keep the common (public) case
# off the database entirely. The per-VIEWER check below is never cached.
_EVENT_PRIVACY_TTL = 60.0
_event_privacy_cache: dict[int, tuple[float, bool]] = {}


def _may_watch_event(user_id, event_id: int) -> bool:
    """Whether ``user_id`` may subscribe to a given event's live frames.

    The HTTP event routes have always refused a private event to outsiders,
    but this stream did not — and its frames are display-ready, naming players,
    task labels, points and team scores. So anyone who knew the id could watch
    a private event live that its own page would 404 for them.

    Runs once per connection, not per frame, so a real lookup is affordable.
    """
    import time as _time

    from db.models import Event
    from web_api.common import db_session
    from web_api.routes.events import _can_view_restricted, _is_restricted

    cached = _event_privacy_cache.get(event_id)
    if cached and cached[0] > _time.monotonic():
        if not cached[1]:
            return True  # public event, no per-viewer check needed
    try:
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if ev is None:
                return False
            restricted = _is_restricted(ev)
            _event_privacy_cache[event_id] = (
                _time.monotonic() + _EVENT_PRIVACY_TTL, restricted
            )
            if not restricted:
                return True
            if user_id is None:
                return False
            return _can_view_restricted(s, user_id, ev)
    except Exception:
        # Fail closed: a private event's frames are the thing being protected.
        return False


def _may_read_thread(user_id, thread_id: int) -> bool:
    """Whether ``user_id`` may subscribe to a chat thread's frames (web96a).

    Runs the same membership check the HTTP chat routes use, once per
    connection. Chat frames carry message bodies, so this fails closed on any
    error — an unreadable thread must never fall through to "public".
    """
    if user_id is None:
        return False
    try:
        from db.models import ChatThread
        from services.chat import resolve_membership
        from web_api.common import db_session

        with db_session() as s:
            thread = s.query(ChatThread).filter(ChatThread.id == thread_id).first()
            if thread is None:
                return False
            return resolve_membership(s, thread, user_id) is not None
    except Exception:
        return False


async def _authorize_channels(raw: str) -> list[str]:
    """Validate + filter requested channels. Drops private ``player:`` scopes
    unless a valid session is present, and ``event:``/``chat:``/``user:`` scopes
    the viewer may not see."""
    user_id = None
    have_session = False
    try:
        user_id = optional_user_id()
        have_session = user_id is not None
    except Exception:
        have_session = False

    out = []
    for ch in (raw or "").split(","):
        ch = ch.strip()
        if not ch:
            continue
        # "event:{id}" is public like "group:*" for PUBLIC events (live event
        # pages, Task 17); private ones are gated below.
        if ch not in _PUBLIC_EXACT_SCOPES and not ch.startswith(
            ("group:", "player:", "npc:", "event:", "chat:", "user:")
        ):
            continue
        if ch.startswith("player:") and not have_session:
            continue  # private feed requires a session
        if ch.startswith("user:"):
            # web96a: unlike the `player:` branch above — which only asks that
            # SOME session exists — this compares identities. A user scope
            # carries that person's badge hints and nobody else may listen.
            if not have_session:
                continue
            try:
                if int(ch.split(":", 1)[1]) != int(user_id):
                    continue
            except (IndexError, ValueError, TypeError):
                continue
        if ch.startswith("chat:"):
            if not have_session:
                continue
            try:
                thread_id = int(ch.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            allowed = await asyncio.to_thread(_may_read_thread, user_id, thread_id)
            if not allowed:
                continue
        if ch.startswith("event:"):
            try:
                event_id = int(ch.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            # Off the event loop: this can hit the database.
            allowed = await asyncio.to_thread(
                _may_watch_event, user_id if have_session else None, event_id
            )
            if not allowed:
                continue
        out.append(ch)
        if len(out) >= _MAX_CHANNELS:
            break
    return out


def _redis_url() -> dict:
    return {
        "host": os.getenv("REDIS_HOST", "127.0.0.1"),
        "port": int(os.getenv("REDIS_PORT", "6379")),
        "db": 0,
        "password": os.getenv("DB_PASS"),
    }


@realtime_bp.get("/feed/recent")
async def feed_recent():
    """Recent site-wide drop-feed history (§UI ticker), newest first, so the
    ticker can hydrate on page load instead of waiting for the next drop."""
    import redis.asyncio as aioredis

    client = aioredis.Redis(**_redis_url())
    try:
        raw = await client.lrange(_FEED_HISTORY_KEY, 0, -1)
    except Exception:
        raw = []
    finally:
        try:
            await client.close()
        except Exception:
            pass

    hidden = await asyncio.to_thread(hidden_player_ids)
    events = []
    for item in raw:
        try:
            parsed = json.loads(item)
            if not isinstance(parsed, dict):
                continue
            # New entries are stored as full typed envelopes
            # (services/realtime.py::publish_feed_event); entries written
            # before the multi-type ticker are bare drop data dicts.
            if parsed.get("v") == 1 and "type" in parsed and isinstance(parsed.get("data"), dict):
                envelope = parsed
            else:
                envelope = {"v": 1, "type": "drop", "scope": "feed", "data": parsed}
            if envelope["data"].get("player_id") in hidden:
                continue
            events.append(envelope)
        except Exception:
            continue
    return jsonify(events)


@realtime_bp.get("/stream")
async def stream():
    channels = await _authorize_channels(request.args.get("channels", "global"))
    if not channels:
        channels = ["global"]
    rt_channels = [f"rt:{c}" for c in channels]

    async def event_source():
        import redis.asyncio as aioredis

        client = aioredis.Redis(**_redis_url())
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(*rt_channels)
            # Initial comment so proxies flush headers immediately.
            yield b": connected\n\n"
            last_beat = asyncio.get_event_loop().time()
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None and message.get("type") == "message":
                    data = message.get("data")
                    if isinstance(data, (bytes, bytearray)):
                        data = data.decode("utf-8", "ignore")
                    # The channel, not the envelope, decides which hiding rules
                    # apply — a connection can hold several scopes at once.
                    if await _is_hidden_event(data, _frame_scope(message.get("channel"))):
                        continue
                    yield f"data: {data}\n\n".encode("utf-8")

                now = asyncio.get_event_loop().time()
                if now - last_beat >= _HEARTBEAT_SECONDS:
                    last_beat = now
                    yield b": ping\n\n"
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            try:
                await pubsub.unsubscribe(*rt_channels)
                await pubsub.close()
                await client.close()
            except Exception:
                pass

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(event_source(), headers=headers)
