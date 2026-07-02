"""Task 05 — typed group-config endpoints.

  GET   /api/v1/groups/{id}/config             (session + group admin) -> flat typed object
  PATCH /api/v1/groups/{id}/config             (session + group admin) -> { ok: true }
  GET   /api/v1/groups/{id}/discord-channels   (session + group admin) -> { channels, cached }

Reads/writes ``group_configurations`` validated against the shared registry
(``web_api/config_registry.py``). The plugin-facing ``load_config`` is untouched.
Every PATCH writes an ``audit_log`` row (actor, group, key, before, after).

``discord-channels`` never talks to Discord itself (the Web API holds no bot
token / gateway connection — every other Discord-touching feature in this
codebase delegates to the bot process instead). The bot caches each guild's
text channels to Redis (``guild:{id}:channels``, ``bots/main.py``); this just
reads that cache. If the bot hasn't populated it yet (or is down), the list
comes back empty and the frontend falls back to manual channel-id entry —
this must never be the only way to set a channel.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import AuditLog, Group, GroupConfiguration
from web_api.common import abort_problem, db_session, private_no_store, _rc
from web_api.config_registry import (
    ConfigValidationError,
    SENSITIVE_KEYS,
    all_config_keys,
    coerce_from_storage,
    coerce_to_storage,
    get_config_field,
)
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

config_bp = Blueprint("v1_config", __name__)


@config_bp.get("/groups/<int:group_id>/config")
async def get_group_config(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            rows = (
                s.query(GroupConfiguration)
                .filter(GroupConfiguration.group_id == group_id)
                .all()
            )
            stored = {r.config_key: r.config_value for r in rows}

            out = {}
            for key in all_config_keys():
                field = get_config_field(key)
                if field is None:
                    continue
                out[key] = coerce_from_storage(field, stored.get(key))
            return out

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@config_bp.patch("/groups/<int:group_id>/config")
async def patch_group_config(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    if not body:
        return jsonify({"ok": True})

    # Pre-validate + coerce all provided keys before touching the DB.
    coerced = {}
    for key, value in body.items():
        field = get_config_field(key)
        if field is None:
            abort_problem(422, "Unknown config key", f"'{key}' is not a valid config key.")
        try:
            coerced[key] = coerce_to_storage(key, value)
        except ConfigValidationError as e:
            abort_problem(422, "Invalid config value", e.detail)

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            for key, new_value in coerced.items():
                row = (
                    s.query(GroupConfiguration)
                    .filter(
                        GroupConfiguration.group_id == group_id,
                        GroupConfiguration.config_key == key,
                    )
                    .first()
                )
                before = row.config_value if row else None
                if row:
                    row.config_value = new_value
                else:
                    s.add(
                        GroupConfiguration(
                            group_id=group_id,
                            config_key=key,
                            config_value=new_value,
                        )
                    )
                # Audit trail (redact sensitive values).
                audit_before = "***" if key in SENSITIVE_KEYS else before
                audit_after = "***" if key in SENSITIVE_KEYS else new_value
                s.add(
                    AuditLog(
                        actor_user_id=user_id,
                        group_id=group_id,
                        action="config.update",
                        target=f"group_configurations.{key}",
                        before=audit_before,
                        after=audit_after,
                    )
                )
            s.commit()

            # Invalidate the shared config cache so the bot/plugin path sees the
            # change promptly.
            try:
                import utils.group_config as gc

                gc.invalidate(group_id)
            except Exception:
                pass
            return True

    await asyncio.to_thread(_apply)
    return jsonify({"ok": True})


@config_bp.get("/groups/<int:group_id>/discord-channels")
async def get_group_discord_channels(group_id: int):
    user_id = current_user_id()

    def _load_guild_id():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                abort_problem(404, "Group not found", f"No group with id {group_id}.")
            return str(group.guild_id) if group.guild_id else None

    guild_id = await asyncio.to_thread(_load_guild_id)
    if not guild_id:
        return private_no_store(jsonify({"channels": [], "cached": False}))

    channels = []
    cached = False
    conn = _rc()
    if conn is not None:
        try:
            raw = conn.get(f"guild:{guild_id}:channels")
            if raw:
                channels = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
                cached = True
        except Exception:
            pass

    return private_no_store(jsonify({"channels": channels, "cached": cached}))
