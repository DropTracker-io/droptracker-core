from quart import Blueprint, jsonify, request, session, abort, render_template
from functools import wraps
from .Event import Event
from typing import Dict

gielinor_race_bp = Blueprint('gielinor_race', __name__)

# Store events instead of games
events: Dict[int, Event] = {}

def authorize_user(f):
    @wraps(f)
    async def decorated_function(group_id, *args, **kwargs):
        user = session.get('user')
        if not user or 'id' not in user:
            abort(401, description="User not authenticated")
        
        if not is_user_authorized(user['id'], group_id):
            abort(403, description="User not authorized for this group")
        
        return await f(group_id, *args, **kwargs)
    return decorated_function

def is_user_authorized(user_id, group_id):
    if str(user_id) == "528746710042804247":
        return True
    return False

@gielinor_race_bp.route('/events', methods=['GET'])
async def list_events():
    active_events = [event.to_dict() for event in events.values() if event.is_active]
    past_events = [event.to_dict() for event in events.values() if not event.is_active]

    return await render_template('gielinor_race_list.html', active_events=active_events, past_events=past_events)

@gielinor_race_bp.route('/events/<int:group_id>', methods=['GET'])
async def event_detail(group_id: int):
    if group_id not in events:
        abort(404, description="Event not found")

    event = events[group_id]
    user = session.get('user')
    is_admin = is_user_authorized(user['id'], group_id) if user else False

    return await render_template('gielinor_race_detail.html', event=event.to_dict(), is_admin=is_admin)

@gielinor_race_bp.route('/events/create', methods=['POST'])
@authorize_user
async def create_event():
    data = await request.json
    group_id = data['group_id']
    group_name = data['group_name']
    board_size = data.get('board_size', 100)

    if group_id in events:
        abort(400, description="Event already exists for this group")

    events[group_id] = Event(group_id, group_name, board_size)
    return jsonify({"success": True, "event": events[group_id].to_dict()})

@gielinor_race_bp.route('/<int:group_id>/add_team', methods=['POST'])
@authorize_user
async def add_team(group_id: int):
    data = await request.json
    if group_id not in events:
        abort(404, description="Event not found")
    events[group_id].game.add_team(data['name'], data['player_ids'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/remove_team', methods=['POST'])
@authorize_user
async def remove_team(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.remove_team(data['name'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/add_player', methods=['POST'])
@authorize_user
async def add_player(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.add_player_to_team(data['team_name'], data['player_id'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/remove_player', methods=['POST'])
@authorize_user
async def remove_player(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.remove_player_from_team(data['team_name'], data['player_id'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/set_board_size', methods=['POST'])
@authorize_user
async def set_board_size(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.set_board_size(data['size'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/add_shop_item', methods=['POST'])
@authorize_user
async def add_shop_item(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.add_shop_item(
            data['name'], data['cost'], data['effect'],
            data['emoji'], data['item_type'], data['cooldown']
        )
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/remove_shop_item', methods=['POST'])
@authorize_user
async def remove_shop_item(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.remove_shop_item(data['name'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/set_team_points', methods=['POST'])
@authorize_user
async def set_team_points(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.set_team_points(data['team_name'], data['points'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/set_team_position', methods=['POST'])
@authorize_user
async def set_team_position(group_id: int):
    data = await request.json
    if group_id in events:
        events[group_id].game.set_team_position(data['team_name'], data['position'])
    return jsonify({"success": True})

@gielinor_race_bp.route('/<int:group_id>/end_event', methods=['POST'])
@authorize_user
async def end_event(group_id: int):
    if group_id in events:
        events[group_id].end_game()
    return jsonify({"success": True})
