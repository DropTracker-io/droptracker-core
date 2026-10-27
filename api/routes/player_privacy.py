"""``GET/POST /player/privacy`` — the RuneLite plugin's privacy switches.

The same four switches as the website's /settings page, on the Discord user
that owns the logged-in account (``users``): ``hidden`` ("Hide me
everywhere": off the site's public surfaces and the global Discord, see
data.submissions.common.player_hidden_globally) and the ping trio that
db.ops.get_formatted_name() reads.

Identity is the account, like ``/player/death_messages``: ``acc_hash`` first,
then the exact name with that hash. An account not linked to a Discord user
has no settings to change, and is told so (409) rather than given a form that
saves nowhere.
"""
import asyncio
from datetime import timedelta

from quart import Blueprint, jsonify, request
from quart_rate_limiter import rate_limit

from api.core import get_db_session
from api.routes.member_messages import _identity, _resolve_player

player_privacy_bp = Blueprint("player_privacy", __name__)

# The website's names (web_api/routes/me.py _BOOL_SETTINGS), so both surfaces
# speak of the same thing.
PRIVACY_SETTINGS = ("hidden", "global_ping", "group_ping", "never_ping")

NOT_LINKED = ("This account isn't linked to a Discord account yet. Claim it with /claim-rsn "
              "in Discord or on droptracker.io, then your privacy settings will show here.")


def _payload(player, user) -> dict:
    return {
        "player_name": player.player_name,
        "settings": {key: bool(getattr(user, key, False)) for key in PRIVACY_SETTINGS},
    }


def _owner(db_session, player):
    from db.models import User

    if player.user_id is None:
        return None
    return db_session.query(User).filter(User.user_id == player.user_id).first()


def _lookup(db_session, values):
    """(player, user) for the request's account; either may be None."""
    player_name, acc_hash = _identity(values)
    player = _resolve_player(db_session, player_name, acc_hash)
    return player, (_owner(db_session, player) if player is not None else None)


def _answer(player, user, payload):
    if player is None:
        return jsonify({"error": "Player not found"}), 404
    if user is None:
        return jsonify({"error": NOT_LINKED}), 409
    return jsonify(payload), 200


@player_privacy_bp.get("/player/privacy")
@rate_limit(limit=30, period=timedelta(seconds=60))
async def get_privacy():
    if not _identity(request.args)[1]:
        return jsonify({"error": "player_name and acc_hash are required"}), 400

    def _load():
        db_session = get_db_session()
        try:
            player, user = _lookup(db_session, request.args)
            return player, user, (_payload(player, user) if user is not None else None)
        finally:
            db_session.close()

    try:
        player, user, payload = await asyncio.to_thread(_load)
    except Exception as e:
        print(f"/player/privacy read failed: {e}")
        return jsonify({"error": "Internal error"}), 500
    return _answer(player, user, payload)


@player_privacy_bp.post("/player/privacy")
@rate_limit(limit=10, period=timedelta(seconds=60))
async def save_privacy():
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "A JSON object body is required"}), 400
    if not _identity(body)[1]:
        return jsonify({"error": "player_name and acc_hash are required"}), 400
    changes = body.get("settings")
    if not isinstance(changes, dict) or not changes:
        return jsonify({"error": "settings must be an object of switches"}), 422
    unknown = sorted(set(changes) - set(PRIVACY_SETTINGS))
    if unknown:
        return jsonify({"error": f"Unknown setting: {', '.join(unknown)}"}), 422
    if not all(isinstance(v, bool) for v in changes.values()):
        return jsonify({"error": "Each setting must be true or false"}), 422

    def _apply():
        db_session = get_db_session()
        try:
            player, user = _lookup(db_session, body)
            if user is None:
                return player, user, None
            for key, value in changes.items():
                setattr(user, key, value)
            db_session.commit()
            return player, user, _payload(player, user)
        except Exception:
            db_session.rollback()
            raise
        finally:
            db_session.close()

    try:
        player, user, payload = await asyncio.to_thread(_apply)
    except Exception as e:
        print(f"/player/privacy save failed: {e}")
        return jsonify({"error": "Internal error"}), 500
    return _answer(player, user, payload)
