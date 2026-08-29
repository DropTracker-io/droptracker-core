"""RSN claim endpoints for the website + Discord Activity.

  GET    /api/v1/me/players/claim-preview  -> read-only claim status for an RSN
  POST   /api/v1/me/players/claim          -> claim an RSN (mirrors /claim-rsn)
  DELETE /api/v1/me/players/{id}/claim     -> unclaim an owned player

Expected outcomes (player not tracked, claimed by someone else, ...) are
returned as `status` in a 200 body — the same convention the group wizard's
guild-status endpoint uses. RFC-7807 problems are reserved for auth/validation
and unexpected errors.

The mutation logic lives in the shared service ``db.player_claims`` (also used
by the Discord commands); these routes only translate HTTP <-> service calls.
Note: ``claimed_by_other`` responses intentionally omit the owning Discord id —
that detail is only surfaced by the Discord command, where the caller can see
the owner in-server anyway.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify, request

from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import current_user_id, json_body, load_user
from web_api.routes.auth import get_cached_profile
from utils.format import normalize_claim_rsn_input

# NOTE: db.* submodules (Player, db.player_claims) are imported lazily inside
# the handlers — the unit-test conftest stubs the `db` package, and this keeps
# the route-wiring tests importable (same pattern as group_admin.py's lazy
# `from db.group_creation import create_web_group`).

player_claims_bp = Blueprint("v1_player_claims", __name__)

# The global fallback group (id 2) is an implementation detail — claiming from
# the website should not advertise "you've been added to group 2".
_HIDDEN_GROUP_IDS = {2}


def _clean_guild_id(raw) -> str | None:
    gid = str(raw or "").strip()
    return gid if gid.isdigit() else None


def _public_group(result: dict) -> dict | None:
    gid = result.get("group_id")
    if gid is None or gid in _HIDDEN_GROUP_IDS:
        return None
    return {"id": gid, "name": result.get("group_name") or f"Group {gid}"}


def _public_player(result: dict) -> dict | None:
    pid = result.get("player_id")
    if pid is None:
        return None
    return {"id": pid, "name": result.get("player_name") or ""}


def _owned_players(s, user_id: int) -> list[dict]:
    from db import Player

    return [
        {"id": p.player_id, "name": p.player_name}
        for p in s.query(Player)
        .filter(Player.user_id == user_id)
        .order_by(Player.player_name.asc())
        .all()
    ]


def _require_discord_id(s, user_id: int) -> str:
    user = load_user(s, user_id)
    if not user or not user.discord_id:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return str(user.discord_id)


@player_claims_bp.get("/me/players/claim-preview")
async def claim_preview():
    user_id = current_user_id()
    rsn = request.args.get("rsn", "")
    if not normalize_claim_rsn_input(rsn):
        abort_problem(422, "Invalid RSN", "Provide a RuneScape name to check.")
    guild_id = _clean_guild_id(request.args.get("guild_id"))

    def _load():
        from db.player_claims import preview_claim

        with db_session() as s:
            discord_id = _require_discord_id(s, user_id)
        result = preview_claim(rsn, discord_id=discord_id, guild_id=guild_id)
        return {
            "status": result["status"],
            "player": _public_player(result),
            "group": _public_group(result),
        }

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@player_claims_bp.post("/me/players/claim")
async def claim_rsn():
    user_id = current_user_id()
    body = await json_body()
    rsn = str(body.get("rsn") or "")
    if not normalize_claim_rsn_input(rsn):
        abort_problem(422, "Invalid RSN", "Provide a RuneScape name to claim.")
    guild_id = _clean_guild_id(body.get("guild_id"))
    profile = get_cached_profile(user_id)

    def _apply():
        from db.player_claims import claim_player

        with db_session() as s:
            discord_id = _require_discord_id(s, user_id)
        result = claim_player(
            rsn,
            discord_id=discord_id,
            username=profile.get("display_name"),
            guild_id=guild_id,
        )
        if result["status"] == "error":
            abort_problem(
                500,
                "Claim failed",
                result.get("message") or "A database error occurred.",
            )
        with db_session() as s:
            players = _owned_players(s, user_id)
        return {
            "status": result["status"],
            "player": _public_player(result),
            # already_yours can now carry a group too: a re-claim re-runs the
            # guild-group attach when the hourly WOM sync would not undo it
            # (db.player_claims._reclaim_attach_group).
            "group": _public_group(result)
            if result["status"] in ("claimed", "already_yours")
            else None,
            "players": players,
        }

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))


@player_claims_bp.delete("/me/players/<int:player_id>/claim")
async def unclaim(player_id: int):
    user_id = current_user_id()

    def _apply():
        from db.player_claims import unclaim_player

        with db_session() as s:
            discord_id = _require_discord_id(s, user_id)
        result = unclaim_player(discord_id=discord_id, player_id=player_id)
        if result["status"] in ("not_found", "not_yours"):
            abort_problem(404, "Not found", "That account is not linked to you.")
        if result["status"] == "error":
            abort_problem(
                500,
                "Unclaim failed",
                result.get("message") or "A database error occurred.",
            )
        with db_session() as s:
            players = _owned_players(s, user_id)
        return {"ok": True, "players": players}

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))
