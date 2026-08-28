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

    if not scope.key_may_read_group(key, group_id):
        if group_id == scope.GLOBAL_GROUP_ID:
            return jsonify({
                "error": "forbidden",
                "detail": "Group 2 is the global pseudo-group and cannot be "
                          "exported. Use /v2/players to enumerate players.",
            }), 403
        return jsonify({
            "error": "forbidden",
            "detail": "This key is not scoped to that group.",
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
        body = {
            "group_id": group_id,
            "count": len(players),
            "sections": ctx["sections"],
            # A full page implies there may be more; a short page is the end.
            "next_cursor": player_ids[-1] if len(player_ids) == limit else None,
            "players": players,
        }
        if "meta" in ctx["sections"]:
            # The group's own standing, not the page's — so it reads the same
            # on page 4 of a roster as on page 1.
            body["group"] = _group_block(session, group_id, ctx, with_all_time=True)
        return body

    return await serve("groups.players", resolve, build)


def _group_block(session, group_id: int, ctx, with_all_time: bool) -> dict:
    """The group's own row: name, member count, monthly GP and rank, lifetime GP."""
    from sqlalchemy import text

    from data_api.group_meta import group_stats
    from web_api.common import get_current_partition

    partition = ctx.get("partition") or get_current_partition()
    stats = group_stats(session, [group_id], partition,
                        with_all_time=with_all_time).get(group_id, {})
    row = session.execute(text(
        "SELECT group_name, date_added FROM groups WHERE group_id = :gid"
    ).bindparams(gid=group_id)).first()
    return {
        "group_id": group_id,
        "name": row[0] if row else None,
        "created": row[1].isoformat() if row and hasattr(row[1], "isoformat") else None,
        **stats,
    }


@groups_bp.route("/groups", methods=["GET"])
async def list_groups():
    """Every group, cursor-paginated. Global keys only.

    A group-scoped key already knows which group it is; enumerating the site
    is the one thing scope exists to prevent, so this is not "its own group
    plus nothing" for those keys — it is simply not theirs to call.
    """
    key = g.api_key
    if key.get("scope") != "global":
        return jsonify({
            "error": "forbidden",
            "detail": "Listing groups requires a global key.",
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

    # serve() calls resolve() to price the request and build() to fill it.
    # The page is fetched once in resolve and reused, because the grouped
    # member count is the expensive part and running it twice would double
    # the cost of every call to this endpoint.
    page: dict = {}

    def resolve(session):
        page["groups"] = scope.group_page(session, cursor, limit)
        # Priced per row, like a page of players.
        return list(range(len(page["groups"])))

    def build(session, _player_ids, ctx):
        groups = page["groups"]
        if "meta" in ctx["sections"] and groups:
            # Monthly standing only. All-time is a per-group derivation (its
            # members' lifetime totals summed), which on a listing would be
            # that work once per row — the opposite of a free section.
            from data_api.group_meta import group_stats
            from web_api.common import get_current_partition

            partition = ctx.get("partition") or get_current_partition()
            stats = group_stats(session, [g["group_id"] for g in groups],
                                partition, with_all_time=False)
            for row in groups:
                row["stats"] = stats.get(row["group_id"])
        return {
            "count": len(groups),
            "next_cursor": groups[-1]["group_id"] if len(groups) == limit else None,
            "groups": groups,
        }

    return await serve("groups.list", resolve, build)
