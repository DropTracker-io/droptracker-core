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
from db.group_rename import (
    GROUP_NAME_CONFIG_KEY,
    GroupRenameError,
    invalidate_group_name_caches,
    normalize_group_name,
    rename_group,
)
from utils import group_config
from web_api.common import abort_problem, db_session, private_no_store, _rc
from web_api.config_registry import (
    MANAGED_KEYS,
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
_LONG_VALUE_BASE_KEYS = {
    "personal_best_embed_boss_list",
    "death_message_variants",
    "drop_message_variants",
    "clog_message_variants",
    "pb_message_variants",
    "notify_pet_duplicate_pets",
}


def _with_mirrors(base_keys):
    """Each key plus its seasonal_/hardcore_ mirrors where the registry has
    one. Derived, not hand-listed: the hardcore mirrors (web151a) were missing
    from the hand-kept set, and a long hardcore pet list hit
    "Data too long for column 'config_value'" on save."""
    from web_api.config_registry import HARDCORE_PREFIX, SEASONAL_PREFIX, get_config_field
    keys = set(base_keys)
    for key in base_keys:
        for prefix in (SEASONAL_PREFIX, HARDCORE_PREFIX):
            field = get_config_field(f"{prefix}{key}")
            if field is not None and field["key"] == key:
                keys.add(f"{prefix}{key}")
    return frozenset(keys)


LONG_VALUE_KEYS = _with_mirrors(_LONG_VALUE_BASE_KEYS)

# Auto-provisioning and the per-group WOM sync rate limit are coupled: see the
# post-write hook in patch_group_config. The cooldown key is owned by
# db.ops.sync_group_from_wom_with_stats, which treats a missing row as
# "never synced".
_AUTO_PROVISION_KEY = "auto_provision_members"
_WOM_SYNC_COOLDOWN_KEY = "last_wom_sync"
# services/auto_nickname.py: turning this on renames members who already claimed.
_AUTO_NICKNAMES_KEY = "auto_set_member_nicknames"


def _auto_provision_turned_on(coerced: dict, before) -> bool:
    """True when this PATCH flips ``auto_provision_members`` from off to on.

    Strictly off->on: re-saving a setting that is already enabled must not
    become a way to clear the WOM sync rate limit on demand. ``before`` is the
    stored string (or None when the row did not exist), so the legacy ``"0"``
    and ``"true"`` spellings are read through the same truthiness rule the bot
    applies (utils/group_config.is_truthy).
    """
    if _AUTO_PROVISION_KEY not in coerced:
        return False
    return group_config.is_truthy(coerced[_AUTO_PROVISION_KEY]) and not group_config.is_truthy(before)


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


def load_group_config(user_id: int, group_id: int) -> dict:
    """GET /groups/{id}/config off the request: every registry key, typed.
    Also behind the plugin's group admin (api/routes/plugin_admin.py), so
    both read through one path. Raises ProblemException (403 for non-admins)."""
    with db_session() as s:
        user = load_user(s, user_id)
        assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

        rows = (
            s.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id)
            .all()
        )
        stored = {r.config_key: _effective_stored_value(r) for r in rows}

        # `group_name` is an editable view of groups.group_name, not a
        # setting of its own — read the canonical column so the editor shows
        # the name the site actually displays. Most groups have no config
        # row at all (the field rendered blank); those that do may hold a
        # value from before renaming was wired up (see db/group_rename.py).
        group_name = s.query(Group.group_name).filter(Group.group_id == group_id).scalar()
        stored[GROUP_NAME_CONFIG_KEY] = group_name or ""

        out = {}
        for key in all_config_keys():
            field = get_config_field(key)
            if field is None:
                continue
            out[key] = coerce_from_storage(field, stored.get(key))
        return out


@config_bp.get("/groups/<int:group_id>/config")
async def get_group_config(group_id: int):
    user_id = current_user_id()
    payload = await asyncio.to_thread(load_group_config, user_id, group_id)
    return private_no_store(jsonify(payload))


def apply_group_config_patch(user_id: int, group_id: int, body: dict) -> None:
    """PATCH /groups/{id}/config off the request: validates every key, then
    writes, audits and invalidates exactly as the site does. Also behind the
    plugin's group admin (api/routes/plugin_admin.py), so a setting saved in
    game goes through the same checks and side effects. Raises
    ProblemException (403 non-admin or missing entitlement, 422 bad value)."""
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

        _check_hof()

    # Pre-validate + coerce all provided keys before touching the DB.
    coerced = {}
    for key, value in body.items():
        field = get_config_field(key)
        if field is None:
            abort_problem(422, "Unknown config key", f"'{key}' is not a valid config key.")
        if key in MANAGED_KEYS:
            abort_problem(422, "Managed setting",
                          "The bot keeps track of this message itself. Use Re-post board to start it over.")
        try:
            coerced[key] = coerce_to_storage(key, value)
        except ConfigValidationError as e:
            abort_problem(422, "Invalid config value", e.detail)

    # boardstyle keys reference lootboards-table rows; the catalog lives in
    # the DB (GET /lootboard-styles), so existence is checked here rather
    # than against the static registry.
    for key in [k for k in coerced if (get_config_field(k) or {}).get("type") == "boardstyle"]:
        if key in coerced:
            try:
                style_id = int(str(coerced[key]).strip())
            except (TypeError, ValueError):
                abort_problem(422, "Invalid config value", f"'{key}' must be a lootboard style id.")

            def _style_exists(sid=style_id):
                with db_session() as s:
                    return s.query(LootboardStyle.id).filter(LootboardStyle.id == sid).first() is not None

            if not _style_exists():
                abort_problem(422, "Invalid config value", f"Unknown lootboard style id {style_id}.")
            coerced[key] = str(style_id)

    # `group_name` renames the group rather than saving a setting, so validate
    # it against the column's own rules before anything is written.
    if GROUP_NAME_CONFIG_KEY in coerced:
        try:
            coerced[GROUP_NAME_CONFIG_KEY] = normalize_group_name(coerced[GROUP_NAME_CONFIG_KEY])
        except GroupRenameError as e:
            abort_problem(422, "Invalid config value", str(e))

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            renamed = False
            # Prior value of auto_provision_members, captured so the post-write
            # hook below can tell an off->on flip from a no-op re-save.
            provision_before = None
            # Same for auto_set_member_nicknames: an off->on flip queues the
            # bot's rename pass over members who claimed before it existed.
            nicknames_before = None
            for key, new_value in coerced.items():
                # `group_name` is the group's actual name rather than a setting
                # of its own: rename_group writes the canonical column, this
                # config row and the forum mirror together, so the new name
                # reaches every surface (db/group_rename.py). Audited against
                # the column it really changes.
                if key == GROUP_NAME_CONFIG_KEY:
                    try:
                        before = rename_group(s, group_id, new_value).before
                    except GroupRenameError as e:
                        abort_problem(422, "Invalid config value", str(e))
                    renamed = True
                    s.add(
                        AuditLog(
                            actor_user_id=user_id,
                            group_id=group_id,
                            action="config.update",
                            target="groups.group_name",
                            before=before,
                            after=new_value,
                        )
                    )
                    continue

                row = (
                    s.query(GroupConfiguration)
                    .filter(
                        GroupConfiguration.group_id == group_id,
                        GroupConfiguration.config_key == key,
                    )
                    .first()
                )
                before = _effective_stored_value(row) if row else None
                if key == _AUTO_PROVISION_KEY:
                    provision_before = before
                elif key == _AUTO_NICKNAMES_KEY:
                    nicknames_before = before
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

            # Turning on auto-provisioning changes what a WOM sync *does* (it
            # starts minting profiles for roster members who have never used the
            # plugin), so the previous sync's 1-hour cooldown must not gate it.
            # Without this, an admin who enabled the setting shortly after any
            # sync got "+0 / -0" from every Sync-from-WOM click for up to an
            # hour and reasonably concluded the setting was broken. Dropping the
            # marker row is what db.ops.sync_group_from_wom_with_stats reads as
            # "never synced". Only on an off->on flip: re-saving an already-on
            # setting must not become a way to bypass the rate limit.
            if _auto_provision_turned_on(coerced, provision_before):
                s.query(GroupConfiguration).filter(
                    GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key == _WOM_SYNC_COOLDOWN_KEY,
                ).delete(synchronize_session=False)

            if _AUTO_NICKNAMES_KEY in coerced:
                from services.auto_nickname import enqueue_backfill_if_turned_on

                enqueue_backfill_if_turned_on(s, group_id, nicknames_before,
                                              coerced[_AUTO_NICKNAMES_KEY],
                                              actor_user_id=user_id)

            s.commit()

            # Invalidate the shared config cache so the bot/plugin path sees the
            # change promptly.
            try:
                import utils.group_config as gc

                gc.invalidate(group_id)
            except Exception:
                pass
            # A rename additionally invalidates the cached pretty-URL slug the
            # name derives from.
            if renamed:
                invalidate_group_name_caches(group_id)
            return True

    _apply()


@config_bp.patch("/groups/<int:group_id>/config")
async def patch_group_config(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    if not body:
        return jsonify({"ok": True})

    await asyncio.to_thread(apply_group_config_patch, user_id, group_id, body)
    return jsonify({"ok": True})


#: Standing boards an admin can have posted afresh: (channel key, message key,
#: how the poster remembers what it last posted).
REPOST_BOARDS = {
    "lootboard": ("lootboard_channel_id", "lootboard_message_id", "loot"),
    "pvp_lootboard": ("pvp_lootboard_channel_id", "pvp_lootboard_message_id", "pvp"),
    "clan_log": ("clan_log_channel_id", "clan_log_message_id", "clan_log"),
}


def repost_board(user_id: int, group_id: int, board: str) -> dict:
    """Start a standing board over in its channel, the way a reset should
    happen now that the message id is not editable (MANAGED_KEYS).

    Forgets the stored message id (keeping the row: the lootboard poster only
    serves groups that have one), asks the bot to delete the old board if, and
    only if, the bot posted it, and drops the poster's "already posted" marker,
    so the next posting pass (about a minute) sends a fresh message. Raises
    ProblemException: 404 unknown board, 409 no channel set."""
    if board not in REPOST_BOARDS:
        abort_problem(404, "Unknown board", f"'{board}' is not a board that can be re-posted.")
    channel_key, message_key, marker = REPOST_BOARDS[board]
    import re

    from services import discord_outbox
    from web_api.config_registry import parse_channel_ref

    with db_session() as s:
        user = load_user(s, user_id)
        assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
        rows = {
            r.config_key: r
            for r in s.query(GroupConfiguration)
            .filter(GroupConfiguration.group_id == group_id,
                    GroupConfiguration.config_key.in_([channel_key, message_key]))
            .order_by(GroupConfiguration.id)
            .all()[::-1]  # lowest id wins, as the posters read it
        }
        channel = parse_channel_ref(rows[channel_key].config_value) if channel_key in rows else None
        if not channel:
            abort_problem(409, "No channel", "Pick a channel for this board first.")
        message_row = rows.get(message_key)
        old = (message_row.config_value or "").strip() if message_row else ""
        if message_row is None:
            s.add(GroupConfiguration(group_id=group_id, config_key=message_key, config_value=""))
        else:
            message_row.config_value = ""
        if re.fullmatch(r"\d{15,21}", old):
            discord_outbox.enqueue(s, channel_id=channel, kind="delete_own_message",
                                   discord_message_id=old, ref_type="board_repost",
                                   actor_user_id=user_id, commit=False)
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id, action="board.repost",
                       target=f"group_configurations.{message_key}", before=old or None, after=""))
        s.commit()

    group_config.invalidate(group_id)
    conn = _rc()
    if conn is not None:
        try:
            if marker == "clan_log":
                conn.delete(f"clan_log:{group_id}:posted_hash")
            else:
                from lootboard import schedule

                conn.delete(schedule.posted_key(group_id, marker))
        except Exception:
            pass
    return {"ok": True, "board": board}


@config_bp.post("/groups/<int:group_id>/boards/<board>/repost")
async def post_repost_board(group_id: int, board: str):
    user_id = current_user_id()
    return jsonify(await asyncio.to_thread(repost_board, user_id, group_id, board))


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


def pb_boss_names():
    """``(names, cached)``: the boss names `personal_best_embed_boss_list` may
    hold (every NPC with a personal best stored), from Redis when it can.
    Also what the plugin's group settings offer (api/routes/plugin_admin.py)."""
    conn = _rc()
    if conn is not None:
        try:
            raw = conn.get(_PB_BOSSES_CACHE_KEY)
            if raw:
                return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw), True
        except Exception:
            pass

    with db_session() as s:
        rows = (
            s.query(NpcList.npc_name)
            .join(PersonalBestEntry, PersonalBestEntry.npc_id == NpcList.npc_id)
            .distinct()
            .order_by(NpcList.npc_name)
            .all()
        )
        names = [r[0] for r in rows]

    if conn is not None:
        try:
            conn.set(_PB_BOSSES_CACHE_KEY, json.dumps(names), ex=_PB_BOSSES_CACHE_TTL)
        except Exception:
            pass
    return names, False


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

    names, cached = await asyncio.to_thread(pb_boss_names)
    return private_no_store(jsonify({"bosses": names, "cached": cached}))
