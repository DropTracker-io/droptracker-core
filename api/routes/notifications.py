"""GET /notifications — the plugin's in-game notification poll.

Drains the per-player Redis inbox written by services/plugin_notifications
(event fan-out + submission notices) and reports whether the player is in a
live event so the plugin knows to keep polling. Identity follows the existing
plugin trust model: player_name + acc_hash, hash-first resolution (same as
/load_config and /panel_data). See docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md.
"""
import asyncio

from quart import Blueprint, jsonify, request

from api.core import get_db_session
from db import Player

# services.* must be lazy-imported inside handlers: the unit-test conftest
# stubs the services package (same rule as web_api routes).

notifications_bp = Blueprint("notifications", __name__)


def _resolve_player_id(db_session, player_name, acc_hash):
    """Canonical identity lookup by account hash first, then strict
    name+hash — mirrors _group_configs_for in api/routes/players.py."""
    player = db_session.query(Player).filter(Player.account_hash == acc_hash).first()
    if not player:
        player = (
            db_session.query(Player)
            .filter(Player.player_name == player_name, Player.account_hash == acc_hash)
            .first()
        )
    return player.player_id if player else None


@notifications_bp.get("/notifications")
async def get_notifications():
    player_name = request.args.get("player_name", None)
    acc_hash = request.args.get("acc_hash", None)
    if not player_name or not acc_hash:
        return jsonify({"error": "player_name and acc_hash are required"}), 400

    from services.plugin_notifications import drain_inbox, player_has_active_event

    def _load():
        db_session = get_db_session()
        try:
            player_id = _resolve_player_id(db_session, player_name, acc_hash)
            if player_id is None:
                return None
            try:
                active_event = player_has_active_event(db_session, player_id)
            except Exception as e:
                print(f"/notifications active-event check failed: {e}")
                active_event = False
            return player_id, active_event
        finally:
            db_session.close()

    try:
        resolved = await asyncio.to_thread(_load)
    except Exception as e:
        print(f"/notifications error: {e}")
        return jsonify({"error": "Internal error"}), 500
    if resolved is None:
        return jsonify({"error": "Player not found"}), 404

    player_id, active_event = resolved
    notifications = await asyncio.to_thread(drain_inbox, player_id)
    return jsonify({"notifications": notifications, "active_event": active_event}), 200
