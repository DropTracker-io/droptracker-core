"""``GET/POST /player/death_messages`` — the RuneLite plugin's member message editor.

Despite the path, every message type: ``type`` (query on GET, body on POST)
picks death, clog, drop or pb, and plugins that send none get deaths, as they
always did. ``/player/member_messages`` is the same handler.

The same messages a member edits on the website and in Discord (``/settings``),
read and written by account: the plugin knows which account is logged in, not
which Discord user owns it. Identity is ``acc_hash`` first, then the exact name
with that hash — the same resolution as ``/notifications`` and ``/load_config``.

Answers carry the limits, the placeholders and, per group, whether the message
would be posted there, so the plugin's editor needs no copy of any rule it can
be told. Validation is ``db.member_messages``, shared with every other surface;
a refused save is a 422 whose ``error`` is written for the player to read.
"""
import asyncio
from datetime import timedelta

from quart import Blueprint, jsonify, request
from quart_rate_limiter import rate_limit

from api.core import get_db_session
from db.member_messages import (
    VIA_PLUGIN,
    MemberMessageError,
    limits_payload,
    load_member_message_row,
    member_message_group_status,
    message_kind,
    parse_stored_messages,
    store_member_messages,
)

member_messages_bp = Blueprint("member_messages", __name__)


def _resolve_player(db_session, player_name, acc_hash):
    from db.models import Player

    player = db_session.query(Player).filter(Player.account_hash == acc_hash).first()
    if player is None and player_name:
        player = (
            db_session.query(Player)
            .filter(Player.player_name == player_name, Player.account_hash == acc_hash)
            .first()
        )
    return player


def _payload(db_session, player, message_type) -> dict:
    row = load_member_message_row(db_session, player.player_id, message_type)
    return {
        **limits_payload(message_type),
        "player_name": player.player_name,
        "messages": (
            parse_stored_messages(row.messages, message_type) if row is not None else []
        ),
        "groups": member_message_group_status(db_session, player.player_id, message_type),
    }


def _message_type(values):
    """The requested type, or None when it is not one we know."""
    try:
        return message_kind((values or {}).get("type")).key
    except MemberMessageError:
        return None


def _identity(values):
    player_name = str(values.get("player_name") or "").strip()
    acc_hash = str(values.get("acc_hash") or "").strip()
    return player_name, acc_hash


@member_messages_bp.get("/player/member_messages")
@member_messages_bp.get("/player/death_messages")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def get_death_messages():
    player_name, acc_hash = _identity(request.args)
    if not acc_hash:
        return jsonify({"error": "player_name and acc_hash are required"}), 400
    message_type = _message_type(request.args)
    if message_type is None:
        return jsonify({"error": "Unknown message type."}), 422

    def _load():
        db_session = get_db_session()
        try:
            player = _resolve_player(db_session, player_name, acc_hash)
            return _payload(db_session, player, message_type) if player is not None else None
        finally:
            db_session.close()

    try:
        payload = await asyncio.to_thread(_load)
    except Exception as e:
        print(f"/player/death_messages read failed: {e}")
        return jsonify({"error": "Internal error"}), 500
    if payload is None:
        return jsonify({"error": "Player not found"}), 404
    return jsonify(payload), 200


@member_messages_bp.post("/player/member_messages")
@member_messages_bp.post("/player/death_messages")
@rate_limit(limit=10, period=timedelta(seconds=60))
async def save_death_messages():
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "A JSON object body is required"}), 400
    player_name, acc_hash = _identity(body)
    if not acc_hash:
        return jsonify({"error": "player_name and acc_hash are required"}), 400
    messages = body.get("messages")
    if not isinstance(messages, list):
        return jsonify({"error": "Messages must be a list of text lines."}), 422
    message_type = _message_type(body)
    if message_type is None:
        return jsonify({"error": "Unknown message type."}), 422

    def _apply():
        db_session = get_db_session()
        try:
            player = _resolve_player(db_session, player_name, acc_hash)
            if player is None:
                return None, None
            try:
                store_member_messages(db_session, player.player_id, messages, via=VIA_PLUGIN,
                                      message_type=message_type)
            except MemberMessageError as exc:
                db_session.rollback()
                return None, str(exc)
            db_session.commit()
            return _payload(db_session, player, message_type), None
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.close()

    try:
        payload, issue = await asyncio.to_thread(_apply)
    except Exception as e:
        print(f"/player/death_messages save failed: {e}")
        return jsonify({"error": "Internal error"}), 500
    if issue is not None:
        return jsonify({"error": issue}), 422
    if payload is None:
        return jsonify({"error": "Player not found"}), 404
    return jsonify(payload), 200
