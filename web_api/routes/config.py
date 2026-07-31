"""Task 05 — typed group-config endpoints.

  GET   /api/v1/groups/{id}/config             (session + group admin) -> flat typed object
  PATCH /api/v1/groups/{id}/config             (session + group admin) -> { ok: true }
  GET   /api/v1/groups/{id}/discord-channels   (session + group admin) -> { channels, cached }
  GET   /api/v1/groups/{id}/pb-bosses          (session + group admin) -> { bosses, cached }

Reads/writes ``group_configurations`` validated against the shared registry
(``web_api/config_registry.py``). The plugin-facing ``load_config`` is untouched.
Every PATCH writes an ``audit_log`` row (actor, group, key, before, after).

``discord-channels`` never talks to Discord itself (the Web API holds no bot
token / gateway connection — every other Discord-touching feature in this
codebase delegates to the bot process instead). The bot caches each guild's
text channels, forums, and active forum threads to Redis
(``guild:{id}:channels``, ``bots/main.py``); this just reads that cache and
asks the bot to re-fetch for next time. If the bot hasn't populated it yet
(or is down), the list comes back empty and the frontend falls back to manual
channel-id entry — this must never be the only way to set a channel. Pasted
ids may be plain channels *or* threads: notification sending goes through
``bot.fetch_channel(id).send(...)``, which works identically for both.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import AuditLog, Group, GroupConfiguration, LootboardStyle, NpcList, PersonalBestEntry
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
    assert_group_entitlement,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)
from web_api.entitlements_registry import HALL_OF_FAME_CONFIG_KEYS
from web_api.routes.event_discord import _request_channel_refresh

config_bp = Blueprint("v1_config", __name__)

# Keys whose values can exceed group_configurations.config_value (VARCHAR(255)).
# The HoF service reads these via long_value when config_value is empty/short
# (services/hall_of_fame.py _parse_group_boss_list), so the typed endpoints
# must round-trip long_value or long lists silently truncate on save.
LONG_VALUE_KEYS = {"personal_best_embed_boss_list"}


@config_bp.get("/seasonal-status")
async def seasonal_status():
    """Whether seasonal-world submission processing is globally enabled.

    Public: the group config editor shows a notice on the Seasonal tab when
    the switch is off (superadmins toggle it between Leagues/DMM seasons).
    """
    from services.seasonal_state import is_seasonal_active

    active = await asyncio.to_thread(is_seasonal_active)
    resp = jsonify({"active": active})
    resp.headers["Cache-Control"] = "public, max-age=30"
    return resp


def _effective_stored_value(row: GroupConfiguration) -> str | None:
    """Mirror the HoF parser's precedence: config_value wins unless it's empty
    or suspiciously short, in which case the overflow long_value is used."""
    value = row.config_value or ""
    if row.config_key in LONG_VALUE_KEYS and len(value) < 10:
        return row.long_value or value
    return row.config_value


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
            stored = {r.config_key: _effective_stored_value(r) for r in rows}

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

    if any(key in HALL_OF_FAME_CONFIG_KEYS for key in body):
        def _check_hof():
            with db_session() as s:
                user = load_user(s, user_id)
                assert_group_entitlement(
                    s,
                    user_id,
                    group_id,
                    "hall_of_fame",
                    manage_guild_ids=manageable_guild_ids(user_id),
                    user=user,
                )

        await asyncio.to_thread(_check_hof)

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

    # boardstyle keys reference lootboards-table rows; the catalog lives in
    # the DB (GET /lootboard-styles), so existence is checked here rather
    # than against the static registry.
    for key in ("loot_board_type", "seasonal_loot_board_type"):
        if key in coerced:
            try:
                style_id = int(str(coerced[key]).strip())
            except (TypeError, ValueError):
                abort_problem(422, "Invalid config value", f"'{key}' must be a lootboard style id.")

            def _style_exists(sid=style_id):
                with db_session() as s:
                    return s.query(LootboardStyle.id).filter(LootboardStyle.id == sid).first() is not None

            if not await asyncio.to_thread(_style_exists):
                abort_problem(422, "Invalid config value", f"Unknown lootboard style id {style_id}.")
            coerced[key] = str(style_id)

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
                before = _effective_stored_value(row) if row else None
                # Overflow-capable keys: the full value always lives in
                # long_value; config_value only holds it when it fits (empty
                # otherwise, so the short-value fallback in the HoF parser
                # kicks in instead of reading a truncated list).
                if key in LONG_VALUE_KEYS:
                    short_value = new_value if len(new_value) <= 255 else ""
                    if row:
                        row.config_value = short_value
                        row.long_value = new_value
                    else:
                        s.add(
                            GroupConfiguration(
                                group_id=group_id,
                                config_key=key,
                                config_value=short_value,
                                long_value=new_value,
                            )
                        )
                elif row:
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
            # An admin touching `recaps_enabled` takes ownership of it: the
            # launch pass sets it on a cohort of clans and later reverts only
            # what it still recognises, so clearing its marker here is what
            # stops that revert from switching off a clan that chose to keep
            # recaps. See scripts/seed_recap_groups.py.
            if "recaps_enabled" in coerced:
                s.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == "recaps_seeded",
                ).delete(synchronize_session=False)

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

    # Always ask the bot to re-fetch this guild (drained within ~15s), so a
    # channel/thread the admin just created shows up on the next page load
    # instead of waiting for the 5-minute sweep. Cheap: one SADD here, one
    # REST fetch bot-side.
    _request_channel_refresh(guild_id)

    return private_no_store(jsonify({"channels": channels, "cached": cached}))


_PB_BOSSES_CACHE_KEY = "web:pb-boss-names"
_PB_BOSSES_CACHE_TTL = 600


@config_bp.get("/groups/<int:group_id>/pb-bosses")
async def get_group_pb_bosses(group_id: int):
    """Boss names eligible for `personal_best_embed_boss_list` — the distinct
    NPC names that have at least one personal best stored, i.e. exactly the
    names the HoF service can match against. Global (not group-scoped), since
    a group may want to track bosses its members haven't submitted PBs for yet.
    Cached in Redis; falls back to a direct query when Redis is unavailable."""
    user_id = current_user_id()

    def _authorize():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

    await asyncio.to_thread(_authorize)

    conn = _rc()
    if conn is not None:
        try:
            raw = conn.get(_PB_BOSSES_CACHE_KEY)
            if raw:
                names = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
                return private_no_store(jsonify({"bosses": names, "cached": True}))
        except Exception:
            pass

    def _load():
        with db_session() as s:
            rows = (
                s.query(NpcList.npc_name)
                .join(PersonalBestEntry, PersonalBestEntry.npc_id == NpcList.npc_id)
                .distinct()
                .order_by(NpcList.npc_name)
                .all()
            )
            return [r[0] for r in rows]

    names = await asyncio.to_thread(_load)

    if conn is not None:
        try:
            conn.set(_PB_BOSSES_CACHE_KEY, json.dumps(names), ex=_PB_BOSSES_CACHE_TTL)
        except Exception:
            pass

    return private_no_store(jsonify({"bosses": names, "cached": False}))
