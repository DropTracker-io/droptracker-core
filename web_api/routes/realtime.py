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

from web_api.common import hidden_player_ids
from web_api.deps import optional_user_id

realtime_bp = Blueprint("v1_realtime", __name__)


async def _is_hidden_event(frame: str) -> bool:
    """True when an rt:* frame belongs to a hidden player (privacy filter for
    the live feed/leaderboard deltas). Best-effort: unparseable or player-less
    frames pass through."""
    try:
        envelope = json.loads(frame)
        # Only these carry a player id in `data` ("id"/"player_id" mean other
        # entities on announcement/event_update frames).
        if envelope.get("type") not in ("drop", "leaderboard_delta"):
            return False
        data = envelope.get("data") or {}
        pid = data.get("player_id", data.get("id"))
        if not isinstance(pid, int):
            return False
        hidden = await asyncio.to_thread(hidden_player_ids)
        return pid in hidden
    except Exception:
        return False

_HEARTBEAT_SECONDS = 20
_MAX_CHANNELS = 24
_PUBLIC_EXACT_SCOPES = ("global", "feed")  # "feed" = site-wide live drop ticker
_FEED_HISTORY_KEY = "feed:recent"


def _authorize_channels(raw: str) -> list[str]:
    """Validate + filter requested channels. Drops private ``player:`` scopes
    unless a valid session is present."""
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
        # "event:{id}" is public like "group:*" (live event pages, Task 17)
        if ch not in _PUBLIC_EXACT_SCOPES and not ch.startswith(("group:", "player:", "npc:", "event:")):
            continue
        if ch.startswith("player:") and not have_session:
            continue  # private feed requires a session
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
            data = json.loads(item)
            if data.get("player_id") in hidden:
                continue
            events.append({"v": 1, "type": "drop", "scope": "feed", "data": data})
        except Exception:
            continue
    return jsonify(events)


@realtime_bp.get("/stream")
async def stream():
    channels = _authorize_channels(request.args.get("channels", "global"))
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
                    if await _is_hidden_event(data):
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
