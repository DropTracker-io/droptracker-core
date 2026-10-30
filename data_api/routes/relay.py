"""``/v2/relay/*`` — the Discord delivery relay, customer-bot side (web153a).

A group's own Discord bot uses these endpoints to deliver DropTracker's
messages in its server. Protocol and outbox: ``services/discord_relay_core``.

    GET  /v2/relay/integration              who am I; guilds, features, the
                                            interaction defer rules
    GET  /v2/relay/messages?wait=&limit=    claim pending deliveries (long-poll)
    POST /v2/relay/messages/<id>/ack        report a delivery's outcome
    GET  /v2/relay/files/<id>/<index>       an attachment's bytes
    POST /v2/relay/interactions             forward a DropTracker button click
    PUT  /v2/relay/channels                 report the bot's channels in a guild
    POST /v2/relay/resync                   redraw the group's live boards now

Only a key bound to an integration (``discord_relay_integrations.api_key_id``)
may call them; any other key gets 403. The data API's DB user is SELECT-only,
so this module only reads the DB — delivery state is Redis — and the relay
endpoints sit outside the tier's read budget (a bot polling every few seconds
would exhaust a budget sized for data queries) under their own per-key
ceiling, :data:`RELAY_REQUESTS_PER_MIN`.
"""
from __future__ import annotations

import asyncio
import os
import time

from quart import Blueprint, Response, g, jsonify, request

from data_api import usage
from data_api.core import SessionLocal

relay_bp = Blueprint("relay", __name__)

RELAY_REQUESTS_PER_MIN = int(os.getenv("DATA_API_RELAY_REQUESTS_PER_MIN", "300"))
WAIT_MAX_SECONDS = 25
WAIT_STEP_SECONDS = 1.0
INTEGRATION_CACHE_SECONDS = 15
MAX_INTERACTION_BYTES = 64 * 1024
MAX_CHANNELS_PER_REPORT = 1000

_integration_cache: dict = {}


def _redis():
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


def _load_integration(key_id: int):
    from db.models import DiscordRelayGuild, DiscordRelayIntegration
    from services import discord_relay_core as core

    s = SessionLocal()
    try:
        integ = (s.query(DiscordRelayIntegration)
                 .filter(DiscordRelayIntegration.api_key_id == key_id).first())
        if integ is None:
            return None
        guilds = [gid for (gid,) in s.query(DiscordRelayGuild.guild_id)
                  .filter(DiscordRelayGuild.integration_id == integ.id).all()]
        return {
            "id": integ.id, "group_id": integ.group_id, "label": integ.label,
            "application_id": integ.application_id, "status": integ.status,
            "features": core.normalize_features(integ.features),
            "hold_minutes": int(integ.hold_minutes or 360),
            "guild_ids": [str(x) for x in guilds],
        }
    finally:
        s.close()


async def _integration():
    key_id = g.api_key["key_id"]
    hit = _integration_cache.get(key_id)
    if hit and time.time() - hit[0] < INTEGRATION_CACHE_SECONDS:
        return hit[1]
    integ = await asyncio.to_thread(_load_integration, key_id)
    _integration_cache[key_id] = (time.time(), integ)
    return integ


def _within_ceiling(conn, key_id: int) -> bool:
    minute = int(time.time()) // 60
    k = f"relay:rl:{key_id}:{minute}"
    try:
        used = conn.incr(k)
        conn.expire(k, 120)
        return int(used) <= RELAY_REQUESTS_PER_MIN
    except Exception:
        return True


async def _guard():
    """``(integration, redis, None)`` or ``(None, None, error_response)``."""
    integ = await _integration()
    if integ is None:
        return None, None, (jsonify({
            "error": "forbidden",
            "detail": "This key is not bound to a Discord relay integration."}), 403)
    conn = _redis()
    if conn is None:
        return None, None, (jsonify({"error": "unavailable"}), 503)
    if not _within_ceiling(conn, g.api_key["key_id"]):
        resp = jsonify({"error": "rate_limited",
                        "detail": f"Relay endpoints allow {RELAY_REQUESTS_PER_MIN} requests/min."})
        resp.headers["Retry-After"] = "10"
        return None, None, (resp, 429)
    return integ, conn, None


def _record(endpoint: str, status: int, started: float) -> None:
    try:
        usage.record(g.api_key["key_id"], endpoint, status,
                     (time.time() - started) * 1000, 0)
    except Exception:
        pass


@relay_bp.route("/relay/integration", methods=["GET"])
async def integration():
    from services import discord_relay_core as core

    started = time.time()
    integ, _conn, err = await _guard()
    if err:
        return err
    _record("relay_integration", 200, started)
    return jsonify({
        "integration_id": integ["id"],
        "group_id": integ["group_id"],
        "label": integ["label"],
        "status": integ["status"],
        "application_id": integ["application_id"],
        "guild_ids": integ["guild_ids"],
        "features": integ["features"],
        "hold_minutes": integ["hold_minutes"],
        # A click whose custom_id starts with one of these prefixes is
        # DropTracker's: defer it IMMEDIATELY with the given mode, then
        # forward the raw interaction to POST /v2/relay/interactions.
        "interaction_rules": [{"prefix": p, "defer": m} for p, m in core.INTERACTION_RULES],
        "poll": {"wait_max": WAIT_MAX_SECONDS, "lease_seconds": core.LEASE_SECONDS},
    })


@relay_bp.route("/relay/messages", methods=["GET"])
async def messages():
    from services import discord_relay_core as core

    started = time.time()
    integ, conn, err = await _guard()
    if err:
        return err
    try:
        limit = int(request.args.get("limit", 20))
        wait = min(max(int(request.args.get("wait", 0)), 0), WAIT_MAX_SECONDS)
    except ValueError:
        return jsonify({"error": "bad_request", "detail": "limit and wait are integers"}), 400
    if integ["status"] != "active":
        # Still honour the long-poll: a bot set up before staff activate it
        # would otherwise spin on instant empty answers into the ceiling.
        if wait:
            await asyncio.sleep(wait)
        _record("relay_messages", 200, started)
        return jsonify({"status": integ["status"], "messages": []})
    deadline = time.time() + wait
    while True:
        batch = await asyncio.to_thread(core.claim, conn, integ["id"], limit,
                                        integ["hold_minutes"])
        if batch or time.time() >= deadline:
            break
        await asyncio.sleep(WAIT_STEP_SECONDS)
    _record("relay_messages", 200, started)
    return jsonify({"status": "active", "messages": batch})


@relay_bp.route("/relay/messages/<int:mid>/ack", methods=["POST"])
async def ack(mid: int):
    from services import discord_relay_core as core

    started = time.time()
    integ, conn, err = await _guard()
    if err:
        return err
    body = await request.get_json(silent=True) or {}
    if not isinstance(body, dict) or "ok" not in body:
        return jsonify({"error": "bad_request", "detail": "body needs 'ok'"}), 400
    try:
        result = await asyncio.to_thread(
            core.ack, conn, integ["id"], mid,
            ok=bool(body.get("ok")),
            discord_message_id=body.get("discord_message_id"),
            http_status=body.get("http_status"), code=body.get("code"),
            error=body.get("error"))
    except LookupError:
        _record("relay_ack", 404, started)
        return jsonify({"error": "not_found"}), 404
    except ValueError as exc:
        _record("relay_ack", 409, started)
        return jsonify({"error": "conflict", "detail": str(exc)}), 409
    _record("relay_ack", 200, started)
    return jsonify(result)


@relay_bp.route("/relay/files/<int:mid>/<int:index>", methods=["GET"])
async def file(mid: int, index: int):
    from services import discord_relay_core as core

    started = time.time()
    integ, conn, err = await _guard()
    if err:
        return err
    data = await asyncio.to_thread(core.get_file, conn, integ["id"], mid, index)
    if data is None:
        _record("relay_file", 404, started)
        return jsonify({"error": "not_found"}), 404
    _record("relay_file", 200, started)
    return Response(data, mimetype="application/octet-stream")


@relay_bp.route("/relay/interactions", methods=["POST"])
async def interactions():
    from services import discord_relay_core as core

    started = time.time()
    integ, conn, err = await _guard()
    if err:
        return err
    if integ["status"] != "active":
        return jsonify({"error": "paused", "detail": "This integration is paused."}), 409
    if (request.content_length or 0) > MAX_INTERACTION_BYTES:
        return jsonify({"error": "too_large"}), 413
    payload = await request.get_json(silent=True)
    if "events" not in integ["features"]:
        return jsonify({"error": "forbidden", "detail": "events are not relayed"}), 403
    mode, problem = core.validate_interaction(payload, integ["application_id"],
                                              integ["guild_ids"])
    if problem:
        _record("relay_interaction", 422, started)
        return jsonify({"error": "rejected", "detail": problem}), 422
    await asyncio.to_thread(core.push_interaction, conn, integ["id"], integ["group_id"],
                            payload, mode)
    _record("relay_interaction", 202, started)
    return jsonify({"accepted": True, "defer": mode}), 202


@relay_bp.route("/relay/channels", methods=["PUT"])
async def channels():
    from services import discord_relay_core as core

    started = time.time()
    integ, conn, err = await _guard()
    if err:
        return err
    body = await request.get_json(silent=True) or {}
    guild_id = str(body.get("guild_id") or "")
    ids = body.get("channel_ids") or []
    if guild_id not in integ["guild_ids"]:
        return jsonify({"error": "forbidden",
                        "detail": "guild is not one this integration owns"}), 403
    if not isinstance(ids, list) or len(ids) > MAX_CHANNELS_PER_REPORT:
        return jsonify({"error": "bad_request",
                        "detail": f"channel_ids: a list of at most {MAX_CHANNELS_PER_REPORT}"}), 400
    n = await asyncio.to_thread(core.remember_channels, conn, guild_id, ids)
    _record("relay_channels", 200, started)
    return jsonify({"remembered": n})


@relay_bp.route("/relay/resync", methods=["POST"])
async def resync():
    from services import discord_relay_core as core

    started = time.time()
    integ, conn, err = await _guard()
    if err:
        return err
    if integ["status"] != "active":
        return jsonify({"error": "paused", "detail": "This integration is paused."}), 409
    body = await request.get_json(silent=True) or {}
    event_id = body.get("event_id")
    if event_id is not None and not str(event_id).isdigit():
        return jsonify({"error": "bad_request", "detail": "event_id must be an integer"}), 400
    await asyncio.to_thread(core.push_resync, conn, integ["id"], integ["group_id"], event_id)
    _record("relay_resync", 202, started)
    return jsonify({"accepted": True}), 202
