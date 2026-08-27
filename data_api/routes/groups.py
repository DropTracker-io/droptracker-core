"""``/v2/groups/...`` — a whole roster, one page at a time.

The bulk endpoint is where the cost model earns its keep: the same
``?include=`` that costs 8 for one player costs 800 for a page of 100, and the
budget refuses it before the database sees it.
"""
from quart import Blueprint, g, jsonify, request

from data_api import scope, sections as sect
from data_api.serving import serve

groups_bp = Blueprint("groups", __name__)

#: Players per page. Above this the per-request cost stops being predictable.
MAX_PAGE = 100
DEFAULT_PAGE = 25


@groups_bp.route("/groups/<int:group_id>/players", methods=["GET"])
async def group_players(group_id: int):
    """One cursor page of a group's members, each with the requested sections.

    ``?cursor=`` is the ``next_cursor`` from the previous response (the last
    player id seen), not an offset.
    """
    key = g.api_key

    if key["owner_type"] != "group" or key["group_id"] != group_id:
        return jsonify({
            "error": "forbidden",
            "detail": "This key is not scoped to that group.",
        }), 403
    if group_id == scope.GLOBAL_GROUP_ID:
        return jsonify({
            "error": "forbidden",
            "detail": "Group 2 is the global pseudo-group and cannot be exported.",
        }), 403

    try:
        limit = int(request.args.get("limit", DEFAULT_PAGE))
    except ValueError:
        limit = DEFAULT_PAGE
    limit = max(1, min(limit, MAX_PAGE))
    try:
        cursor = int(request.args.get("cursor", 0))
    except ValueError:
        cursor = 0

    def resolve(session):
        return scope.group_roster_page(session, group_id, cursor, limit)

    def build(session, player_ids, ctx):
        loaded = sect.load_sections(session, ctx["sections"], player_ids, ctx)
        players = [loaded[pid] for pid in player_ids]
        return {
            "group_id": group_id,
            "count": len(players),
            "sections": ctx["sections"],
            # A full page implies there may be more; a short page is the end.
            "next_cursor": player_ids[-1] if len(player_ids) == limit else None,
            "players": players,
        }

    return await serve("groups.players", resolve, build)
