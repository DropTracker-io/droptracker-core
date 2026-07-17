"""Plugin event endpoints: notifications poll, HUD state, board image.

- GET /notifications — drains the per-player Redis inbox written by
  services/plugin_notifications (event fan-out + submission notices) and
  reports whether the player is in a live event so the plugin keeps polling.
- GET /event_state — the Enhanced Display HUD / Events-tab state (focus task,
  standings, team info) for every active event the player is rostered in.
- GET /events/<id>/board.png — the server-rendered board (bingo grid /
  board-game track), team-scoped, roster-gated, Redis-cached.

Identity follows the existing plugin trust model everywhere: player_name +
acc_hash, hash-first resolution (same as /load_config and /panel_data).
See docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md.
"""
import asyncio
from datetime import timedelta

from quart import Blueprint, Response, jsonify, request
from quart_rate_limiter import rate_limit

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


@notifications_bp.get("/event_state")
async def get_event_state():
    """HUD / Events-tab state for every active event the player is in."""
    player_name = request.args.get("player_name", None)
    acc_hash = request.args.get("acc_hash", None)
    if not player_name or not acc_hash:
        return jsonify({"error": "player_name and acc_hash are required"}), 400

    from services.plugin_notifications import compose_event_state

    def _load():
        db_session = get_db_session()
        try:
            player_id = _resolve_player_id(db_session, player_name, acc_hash)
            if player_id is None:
                return None
            return compose_event_state(db_session, player_id)
        finally:
            db_session.close()

    try:
        state = await asyncio.to_thread(_load)
    except Exception as e:
        print(f"/event_state error: {e}")
        return jsonify({"error": "Internal error"}), 500
    if state is None:
        return jsonify({"error": "Player not found"}), 404
    return jsonify(state), 200


@notifications_bp.get("/events/<int:event_id>/board.png")
@rate_limit(limit=10, period=timedelta(seconds=60))
async def get_event_board_png(event_id: int):
    """Server-rendered board image for the plugin's board pop-out.

    ``team_id`` (optional) renders that team's tab-selected view; omitted =
    the all-teams view. Roster-gated: the caller must be a member of the
    event (covers private events, which the plugin cannot see via the web
    session model). Served from the same Redis render-cache the Discord team
    board posts use, so repeat views don't re-screenshot."""
    player_name = request.args.get("player_name", None)
    acc_hash = request.args.get("acc_hash", None)
    if not player_name or not acc_hash:
        return jsonify({"error": "player_name and acc_hash are required"}), 400
    raw_team = (request.args.get("team_id") or "").strip()
    team_id = None
    if raw_team:
        try:
            team_id = int(raw_team)
        except ValueError:
            return jsonify({"error": "team_id must be an integer"}), 422

    def _gate():
        """Resolve identity + membership; returns the Event or an error code."""
        from db.models import Event, EventTeam, EventTeamMember

        db_session = get_db_session()
        try:
            player_id = _resolve_player_id(db_session, player_name, acc_hash)
            if player_id is None:
                return "player_not_found"
            event = db_session.query(Event).filter(Event.id == event_id).first()
            if event is None:
                return "event_not_found"
            member = (
                db_session.query(EventTeamMember.player_id)
                .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                .filter(EventTeam.event_id == event_id,
                        EventTeamMember.player_id == player_id)
                .first()
            )
            if member is None:
                return "not_rostered"
            if team_id is not None:
                team = (
                    db_session.query(EventTeam.id)
                    .filter(EventTeam.id == team_id,
                            EventTeam.event_id == event_id)
                    .first()
                )
                if team is None:
                    return "team_not_found"
            # Detach a plain snapshot; the render call opens its own session.
            return {"id": event.id}
        finally:
            db_session.close()

    gate = await asyncio.to_thread(_gate)
    if gate == "player_not_found":
        return jsonify({"error": "Player not found"}), 404
    if gate in ("event_not_found", "not_rostered"):
        # Same body for both: don't leak private events' existence.
        return jsonify({"error": "Event not found"}), 404
    if gate == "team_not_found":
        return jsonify({"error": "Team not found in this event"}), 404

    from services.event_board_image import board_image_with_hash

    db_session = get_db_session()
    try:
        from db.models import Event

        event = db_session.query(Event).filter(Event.id == event_id).first()
        png, _state_hash, _rendered = await board_image_with_hash(
            db_session, event, team_id)
    except Exception as e:
        print(f"/events/{event_id}/board.png error: {e}")
        return jsonify({"error": "Board render failed"}), 502
    finally:
        db_session.close()

    if not png:
        return jsonify({"error": "This event has no visual board"}), 404
    return Response(png, content_type="image/png", headers={
        "Cache-Control": "private, max-age=60",
        "Content-Disposition": f'inline; filename="board-{event_id}.png"',
    })
