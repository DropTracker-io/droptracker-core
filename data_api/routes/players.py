"""``/v2/players/...`` — one player, any combination of sections."""
from quart import Blueprint, g, jsonify, request

from data_api import scope, sections as sect
from data_api.serving import serve

players_bp = Blueprint("players", __name__)


@players_bp.route("/players/<ref>", methods=["GET"])
async def get_player(ref: str):
    """``ref`` is a player id or an exact RSN.

    Out-of-scope and hidden players both 404 — telling a caller "that player
    exists but is not yours" would leak the roster of every other clan.
    """
    key = g.api_key

    def resolve(session):
        player_id = scope.resolve_player_ref(session, ref)
        if player_id is None:
            return None
        if not scope.visible_player_ids(session, [player_id]):
            return None
        if not scope.key_may_read(session, key, player_id):
            return None
        return [player_id]

    def build(session, player_ids, ctx):
        loaded = sect.load_sections(session, ctx["sections"], player_ids, ctx)
        return {"player": loaded[player_ids[0]], "sections": ctx["sections"]}

    return await serve("players.get", resolve, build)


@players_bp.route("/sections", methods=["GET"])
async def list_sections():
    """What ``?include=`` accepts, and what each section costs.

    Published so a consumer can budget a call before making it rather than
    discovering the cost from a 429.
    """
    return jsonify({
        "sections": [
            {"key": s.key, "cost_per_player": s.cost, "description": s.description}
            for s in (sect.REGISTRY[k] for k in sect.ALL_SECTION_KEYS)
        ],
        "default": list(sect.DEFAULT_SECTIONS),
        "cost_model": "request cost = players x sum(cost_per_player of each "
                      "requested section); charged against the key's "
                      "cost_units_per_min budget before the query runs.",
        "your_limits": g.api_key["limits"],
    })
