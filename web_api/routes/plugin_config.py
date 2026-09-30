"""Players' plugin settings, as their DropTracker plugin last reported them.

  GET /api/v1/groups/{id}/members/{player_id}/plugin-config   group admin
  GET /api/v1/admin/players/{player_id}/plugin-config         support staff

Backed by ``player_plugin_config`` (config_snapshot submissions, plugin
6.0.16+). A group admin sees only players who are members of that group.
The data is self-reported by the client, and the page should say so.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import Group, Player
from db.models import PlayerPluginConfig
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    assert_support_staff,
    current_user_id,
    load_user,
    manageable_guild_ids,
)

plugin_config_bp = Blueprint("v1_plugin_config", __name__)


def _loads(raw):
    try:
        value = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _flat(snapshot) -> dict:
    """{key: value} across every section of a snapshot's settings."""
    out = {}
    for values in ((snapshot or {}).get("settings") or {}).values():
        if isinstance(values, dict):
            out.update(values)
    return out


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def payload(player, row) -> dict:
    """The response body; ``row`` may be None (no snapshot yet)."""
    body = {"player": {"id": player.player_id, "name": player.player_name}, "snapshot": None}
    if row is None:
        return body
    current = _loads(row.config_json)
    previous = _loads(row.previous_config_json)
    changed = []
    if previous is not None:
        now, before = _flat(current), _flat(previous)
        changed = [
            {"key": key, "from": before.get(key), "to": now.get(key)}
            for key in sorted(set(now) | set(before))
            if now.get(key) != before.get(key)
        ]
    body["snapshot"] = {
        "captured_at": _iso(row.captured_at),
        "plugin_version": row.plugin_version,
        "runelite_version": row.runelite_version,
        "transport": None if row.used_api is None else ("api" if row.used_api else "webhook"),
        "settings": (current or {}).get("settings") or {},
        "customized": (current or {}).get("customized") or [],
        "env": (current or {}).get("env") or {},
        "previous_captured_at": _iso(row.previous_captured_at),
        "changed_since_previous": changed,
    }
    return body


def _player_or_404(s, player_id: int):
    player = s.query(Player).filter(Player.player_id == player_id).first()
    if player is None:
        abort_problem(404, "Player not found", f"No player with id {player_id}.")
    return player


@plugin_config_bp.get("/groups/<int:group_id>/members/<int:player_id>/plugin-config")
async def group_member_plugin_config(group_id: int, player_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            player = _player_or_404(s, player_id)
            member = (
                s.query(Player.player_id)
                .join(Player.groups)
                .filter(Group.group_id == group_id, Player.player_id == player_id)
                .first()
            )
            if member is None:
                abort_problem(404, "Not a member", "That player is not a member of this group.")
            return payload(player, s.get(PlayerPluginConfig, player_id))

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@plugin_config_bp.get("/admin/players/<int:player_id>/plugin-config")
async def staff_player_plugin_config(player_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            assert_support_staff(load_user(s, user_id))
            player = _player_or_404(s, player_id)
            return payload(player, s.get(PlayerPluginConfig, player_id))

    return private_no_store(jsonify(await asyncio.to_thread(_load)))
