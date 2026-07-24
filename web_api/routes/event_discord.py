"""Task 19 — per-event Discord destinations (events-prd.md D8).

  GET /api/v1/events/{id}/discord                      -> EventChannelConfig
  PUT /api/v1/events/{id}/discord                      -> EventChannelConfig
  GET /api/v1/events/discord/guilds                    -> { guilds, stale }
  GET /api/v1/events/discord/guilds/{gid}/channels     -> { channels, stale }
  GET /api/v1/events/discord/guilds/{gid}/roles        -> { roles, stale }

EventChannelConfig also carries ``discord_event_policy`` (when the mirrored
Discord scheduled event goes live: 'on_activate' default / 'immediate') and
``pings`` ({ping_key: [role ids]}, EVENT_PING_KEYS) — both editable on the
same PUT.

Every event can target *any* guild the bot is a member of — including
dedicated event servers — with one channel per notification kind
(announcements / completions / leaderboard / admin; unset kinds fall back to
announcements at send time, services/event_notifications.py).

Auth: event admin (group owner/admin with the events entitlement; superadmin
for global events). The guild/channel *browse* endpoints aren't tied to a
single event, so they accept any signed-in user who administers at least one
group.

The Web API never talks to Discord itself (no bot token / gateway here —
established pattern from routes/config.py). Guild and channel lists come from
bot-maintained Redis caches (`bot:guilds`, `guild:{id}:channels`,
bots/main.py); a cache miss returns `stale: true` + an empty list (the UI
falls back to manual-id entry) and enqueues `bot:channels:refresh` so the bot
warms that guild's channel cache within seconds.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify, request

from db import (
    AuditLog,
    Event,
    EventChannel,
    EventGroup,
    EventGuild,
    EventTeam,
    EventTeamDiscord,
    EventTeamMember,
    Group,
    GroupAdmin,
    Player,
    EVENT_CHANNEL_KINDS,
    EVENT_DISCORD_POLICIES,
    EVENT_MESSAGE_TOGGLE_KEYS,
    EVENT_TASK_PROGRESS_MODES,
    EVENT_TEAM_DISCORD_RETENTIONS,
    EVENT_TEAM_ROLES,
)
from web_api.common import abort_problem, db_session, private_no_store, _rc
from web_api.deps import (
    current_user_id,
    event_manager_group_ids,
    is_event_manager,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
    resolve_group_role,
)
from web_api.routes.events import (
    _assert_event_admin,
    _bump,
    _event_pings,
    _load_event_or_404,
    _parse_ping_config,
    _sync_event_guilds,
    participating_group_ids,
)

event_discord_bp = Blueprint("v1_event_discord", __name__)

# Bot-maintained caches (bots/main.py). Module-level so the verification
# script can rebind them to throwaway test keys.
_BOT_GUILDS_KEY = "bot:guilds"
_CHANNEL_REFRESH_KEY = "bot:channels:refresh"


def _channels_key(guild_id: str) -> str:
    return f"guild:{guild_id}:channels"


def _roles_key(guild_id: str) -> str:
    return f"guild:{guild_id}:roles"


def _read_json_cache(key: str):
    """Parsed JSON at ``key``, or None on miss / Redis trouble (== stale)."""
    conn = _rc()
    if conn is None:
        return None
    try:
        raw = conn.get(key)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


def _request_channel_refresh(guild_id: str) -> None:
    """Ask the bot to warm one guild's channel cache (drained every ~15s)."""
    conn = _rc()
    if conn is None:
        return
    try:
        conn.sadd(_CHANNEL_REFRESH_KEY, str(guild_id))
        conn.expire(_CHANNEL_REFRESH_KEY, 300)
    except Exception:
        pass


def _bot_guilds():
    """[{id, name, icon}] from the bot cache, or None when cold."""
    guilds = _read_json_cache(_BOT_GUILDS_KEY)
    return guilds if isinstance(guilds, list) else None


def _guild_channels(guild_id: str):
    """[{id, name, position}] from the bot cache, or None when cold."""
    channels = _read_json_cache(_channels_key(guild_id))
    return channels if isinstance(channels, list) else None


def _guild_roles(guild_id: str):
    """[{id, name, position}] from the bot cache (`guild:{id}:roles`, written
    alongside the channel cache by ``cache_channels_for_guild``), or None when
    cold."""
    roles = _read_json_cache(_roles_key(guild_id))
    return roles if isinstance(roles, list) else None


def _assert_any_event_admin(s, user_id: int) -> None:
    """Browse-level gate for the guild/channel list endpoints: superadmin, or
    admin of at least one group (MANAGE_GUILD on a linked guild, or an
    explicit web grant). Per-event writes still go through
    ``_assert_event_admin`` which also checks the events entitlement."""
    user = load_user(s, user_id)
    if is_superadmin(user):
        return
    manage_ids = manageable_guild_ids(user_id)
    if manage_ids:
        linked = (
            s.query(Group.group_id)
            .filter(Group.guild_id.in_([str(g) for g in manage_ids]))
            .first()
        )
        if linked:
            return
    grant = s.query(GroupAdmin).filter(GroupAdmin.user_id == user_id).first()
    if grant:
        return
    # web64a: event managers browse their group's guild/channel lists too.
    if event_manager_group_ids(s, user_id):
        return
    abort_problem(403, "Forbidden", "Event admin access is required.")


def _targetable_guild_ids(s, user_id: int):
    """Guild ids this user is allowed to target for event posting, or ``None``
    for "all guilds" (superadmin — they run global events targeting any guild).

    A guild qualifies when the user either **manages it on Discord** (owner or
    MANAGE_GUILD, via the OAuth ``guilds`` scope cached at login — this covers
    a brand-new server they added the bot to, with no DropTracker group) **or
    administers the DropTracker group linked to it** (covers a cold OAuth
    cache and web-granted admins for their clan's home server). Both signals
    are the same ones :func:`resolve_group_role` already trusts app-wide, so
    this introduces no new trust — it only *restricts* the old behaviour of
    exposing every guild the bot is in.
    """
    user = load_user(s, user_id)
    if is_superadmin(user):
        return None
    allowed = {str(g) for g in manageable_guild_ids(user_id)}
    # Add the home guilds of groups the user has an explicit web grant on, or
    # manages events for (web64a) — bounded to this user's grants.
    grant_group_ids = list({
        gid for (gid,) in s.query(GroupAdmin.group_id)
        .filter(GroupAdmin.user_id == user_id).all()
    } | event_manager_group_ids(s, user_id))
    if grant_group_ids:
        for guild_id, in (
            s.query(Group.guild_id)
            .filter(Group.group_id.in_(grant_group_ids))
            .all()
        ):
            if guild_id:
                allowed.add(str(guild_id))
    return allowed


def _assert_can_target_guild(s, user_id: int, guild_id: str) -> None:
    """403 unless the user may post events to ``guild_id`` (see
    :func:`_targetable_guild_ids`). The real security boundary — the dropdown
    filter is only cosmetic; this is what stops a crafted request (or the
    manual-id field) from aiming an event at a guild the user has no authority
    over."""
    allowed = _targetable_guild_ids(s, user_id)
    if allowed is None or str(guild_id) in allowed:
        return
    abort_problem(
        403,
        "Not your server",
        "You can only post events to a Discord server you manage (Server "
        "Manage permission) or whose DropTracker group you administer. If you "
        "recently added the bot to a new server, sign out and back in to "
        "refresh your server list.",
    )


def _guild_name(guild_id) -> str | None:
    if not guild_id:
        return None
    for g in _bot_guilds() or []:
        if str(g.get("id")) == str(guild_id):
            return g.get("name")
    return None


def _admin_participating_group_ids(s, user_id: int, ev: Event) -> set[int]:
    """Accepted participating clans the user administers (clan_vs_clan)."""
    user = load_user(s, user_id)
    mgids = manageable_guild_ids(user_id)
    return {
        gid for gid in participating_group_ids(s, ev)
        if resolve_group_role(s, user_id, gid, mgids, user=user) in ("owner", "admin")
        or is_event_manager(s, user_id, gid)
    }


def _is_host_admin(s, user_id: int, ev: Event) -> bool:
    """Whether the user administers the HOST group (or is superadmin) — the
    only people who may flip event-wide knobs like per_group_discord."""
    user = load_user(s, user_id)
    if is_superadmin(user):
        return True
    if not ev.group_id:
        return False
    role = resolve_group_role(s, user_id, ev.group_id, manageable_guild_ids(user_id), user=user)
    # web64a: host-group event managers may flip the event-wide Discord knobs too.
    return role in ("owner", "admin") or is_event_manager(s, user_id, ev.group_id)


def _config_payload(s, ev: Event, group_id: int | None = None) -> dict:
    """One Discord config scope. ``group_id=None`` is the shared/host scope
    (the only shape before web48a); a group id is that clan's own per-group
    scope (Event.per_group_discord)."""
    rows = (s.query(EventChannel)
            .filter(EventChannel.event_id == ev.id)
            .all())
    channels = {
        r.kind: str(r.channel_id) for r in rows
        if r.channel_id and (getattr(r, "group_id", None) or None) == group_id
    }
    part = None
    if group_id is not None:
        part = (s.query(EventGroup)
                .filter(EventGroup.event_id == ev.id, EventGroup.group_id == group_id)
                .first())
        guild_id = str(part.discord_guild_id) if part and part.discord_guild_id else None
        messages = _effective_message_config(
            part.message_config if part and part.message_config
            else getattr(ev, "message_config", None))
    else:
        guild_id = str(ev.discord_guild_id) if ev.discord_guild_id else None
        messages = _effective_message_config(getattr(ev, "message_config", None))
    # Discord scheduled-event mirror state (web_event_guilds, written back by
    # the bot reconciler) so the UI can show e.g. "couldn't create the Discord
    # event — grant the bot Manage Events" instead of failing silently.
    scheduled_event = None
    if guild_id and group_id is None:
        row = (
            s.query(EventGuild)
            .filter(EventGuild.event_id == ev.id, EventGuild.guild_id == guild_id)
            .first()
        )
        if row:
            scheduled_event = {
                "id": row.discord_scheduled_event_id,
                "status": row.sync_status,
                "last_error": row.last_error if row.sync_status == "failed" else None,
            }
    return {
        "guild_id": guild_id,
        "guild_name": _guild_name(guild_id),
        "channels": channels,
        "scheduled_event": scheduled_event,
        # When the mirror goes live (on_activate: nothing while a draft) and
        # which roles each ping key mentions — both edited on this same PUT.
        # Event-level even in a group scope (pings only fire on the host's
        # destination; the UI hides them for group scopes).
        "discord_event_policy": getattr(ev, "discord_event_policy", None) or "on_activate",
        "pings": _event_pings(ev),
        # Messaging verbosity + live board knobs, always returned fully
        # merged with the defaults (the UI never needs its own default table).
        # Lazy import: the unit-test conftest stubs `services` (established
        # pattern — see services/event_notifications.py module docstring).
        "messages": messages,
        # web48a scope context for the UI.
        "per_group_discord": bool(getattr(ev, "per_group_discord", False)),
        "group_id": group_id,
    }


def _effective_message_config(raw):
    from services.event_notifications import effective_message_config

    return effective_message_config(raw)


def _parse_message_config(value) -> str:
    """Validate + normalize the PUT ``messages`` object into the JSON stored
    on ``web_events.message_config``. Unknown toggle keys are rejected (a
    typo silently doing nothing is worse than a 422); the stored document is
    the fully-merged effective config, so reads never depend on defaults
    changing under an event mid-flight."""
    from services.event_notifications import LEADERBOARD_TOP_N_RANGE

    if not isinstance(value, dict):
        abort_problem(422, "Invalid messages", "'messages' must be an object.")

    toggles = value.get("toggles") or {}
    if not isinstance(toggles, dict):
        abort_problem(422, "Invalid messages", "'messages.toggles' must be an object.")
    for key, enabled in toggles.items():
        if key not in EVENT_MESSAGE_TOGGLE_KEYS:
            abort_problem(
                422, "Unknown message toggle",
                f"'{key}' is not one of {list(EVENT_MESSAGE_TOGGLE_KEYS)}.",
            )
        if not isinstance(enabled, bool):
            abort_problem(422, "Invalid message toggle", f"'messages.toggles.{key}' must be a boolean.")

    mode = value.get("task_progress")
    if mode is not None and mode not in EVENT_TASK_PROGRESS_MODES:
        abort_problem(
            422, "Invalid task progress mode",
            f"messages.task_progress must be one of {list(EVENT_TASK_PROGRESS_MODES)}.",
        )

    if "item_details" in value and not isinstance(value["item_details"], bool):
        abort_problem(422, "Invalid messages", "'messages.item_details' must be a boolean.")

    board = value.get("leaderboard") or {}
    if not isinstance(board, dict):
        abort_problem(422, "Invalid messages", "'messages.leaderboard' must be an object.")
    for flag in ("live", "show_tasks"):
        if flag in board and not isinstance(board[flag], bool):
            abort_problem(422, "Invalid leaderboard option", f"'messages.leaderboard.{flag}' must be a boolean.")
    if "top_n" in board:
        lo, hi = LEADERBOARD_TOP_N_RANGE
        if not isinstance(board["top_n"], int) or isinstance(board["top_n"], bool) \
                or not (lo <= board["top_n"] <= hi):
            abort_problem(
                422, "Invalid leaderboard size",
                f"messages.leaderboard.top_n must be an integer between {lo} and {hi}.",
            )

    return json.dumps(_effective_message_config(value))


def _clean_snowflake(value, what: str) -> str:
    """Snowflakes travel as strings (JS numbers lose precision past 2^53)."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip().isdigit() or len(value.strip()) > 32:
        abort_problem(422, f"Invalid {what}", f"'{what}' must be a Discord snowflake id string.")
    return value.strip()


# --------------------------------------------------------------------------- #
# Browse: guilds the bot is in / channels of one guild
# --------------------------------------------------------------------------- #
@event_discord_bp.get("/events/discord/guilds")
async def list_discord_guilds():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _assert_any_event_admin(s, user_id)
            # Only surface guilds the user is actually allowed to target
            # (None => superadmin, unfiltered).
            allowed = _targetable_guild_ids(s, user_id)
        guilds = _bot_guilds()
        if guilds is None:
            return {"guilds": [], "stale": True}
        return {
            "guilds": [
                {"id": str(g.get("id")), "name": g.get("name") or "", "icon": g.get("icon")}
                for g in guilds
                if g.get("id") and (allowed is None or str(g.get("id")) in allowed)
            ],
            "stale": False,
        }

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_discord_bp.get("/events/discord/guilds/<guild_id>/channels")
async def list_discord_guild_channels(guild_id: str):
    user_id = current_user_id()
    guild_id = _clean_snowflake(guild_id, "guild_id")

    def _load():
        with db_session() as s:
            _assert_any_event_admin(s, user_id)
            # No enumerating channels of a guild the user can't target
            # (a 403 here just drops the UI to manual channel-id entry).
            _assert_can_target_guild(s, user_id, guild_id)
        channels = _guild_channels(guild_id)
        # Always ask the bot to re-fetch (drained within ~15s): on a cold
        # cache that makes the retry succeed shortly; on a warm one it picks
        # up channels/threads created since the last 5-minute sweep.
        _request_channel_refresh(guild_id)
        if channels is None:
            # Cold cache: hand the UI its manual-id fallback meanwhile.
            return {"channels": [], "stale": True}
        return {"channels": channels, "stale": False}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_discord_bp.get("/events/discord/guilds/<guild_id>/roles")
async def list_discord_guild_roles(guild_id: str):
    """Roles of one guild, for the event ping-role pickers. Same cache
    pipeline and auth as the channel browse: the bot maintains
    `guild:{id}:roles` next to the channel cache, and a cold cache returns
    `stale: true` while `bot:channels:refresh` warms both within ~15s."""
    user_id = current_user_id()
    guild_id = _clean_snowflake(guild_id, "guild_id")

    def _load():
        with db_session() as s:
            _assert_any_event_admin(s, user_id)
            _assert_can_target_guild(s, user_id, guild_id)
        roles = _guild_roles(guild_id)
        _request_channel_refresh(guild_id)
        if roles is None:
            return {"roles": [], "stale": True}
        return {"roles": roles, "stale": False}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Per-event config
# --------------------------------------------------------------------------- #
def _assert_group_scope(s, user_id: int, ev: Event, group_id: int) -> None:
    """Gate for a per-group config scope: clan_vs_clan only, the group must be
    an accepted participant, and the user must administer THAT group (each
    clan touches only its own destinations). Superadmins pass."""
    if (getattr(ev, "mode", None) or "standard") != "clan_vs_clan":
        abort_problem(409, "Not clan-vs-clan",
                      "Per-group Discord config only exists on clan-vs-clan events.")
    if group_id not in participating_group_ids(s, ev):
        abort_problem(404, "Not a participant",
                      f"Group {group_id} is not an accepted participant of this event.")
    if is_superadmin(load_user(s, user_id)):
        return
    if group_id not in _admin_participating_group_ids(s, user_id, ev):
        abort_problem(403, "Not your clan",
                      "You can only configure Discord for a clan you administer.")


def _group_scopes(s, ev: Event) -> list:
    """Participating-clan scope list for the host UI: who's in, whether they
    have configured their own channels, and their chosen guild."""
    configured = {
        gid for (gid,) in
        s.query(EventChannel.group_id)
        .filter(EventChannel.event_id == ev.id, EventChannel.group_id.isnot(None))
        .distinct()
    }
    rows = (
        s.query(EventGroup, Group.group_name)
        .join(Group, Group.group_id == EventGroup.group_id)
        .filter(EventGroup.event_id == ev.id, EventGroup.status == "accepted")
        .order_by(EventGroup.id.asc())
        .all()
    )
    return [
        {
            "group_id": g.group_id,
            "name": name,
            "role": g.role,
            "configured": g.group_id in configured,
            "guild_id": str(g.discord_guild_id) if getattr(g, "discord_guild_id", None) else None,
        }
        for g, name in rows
    ]


@event_discord_bp.get("/events/<int:event_id>/discord")
async def get_event_discord(event_id: int):
    """One config scope: shared/host by default, or a participating clan's own
    scope via ``?group_id=`` (web48a per-group discord). The shared scope of a
    clan-vs-clan event additionally lists the participating-clan scopes and
    which of them the viewer administers, so the UI can route."""
    user_id = current_user_id()
    raw_group = (request.args.get("group_id") or "").strip()
    scope_group_id = int(raw_group) if raw_group.isdigit() else None

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            if scope_group_id is not None:
                _assert_group_scope(s, user_id, ev, scope_group_id)
                return _config_payload(s, ev, group_id=scope_group_id)
            _assert_event_admin(s, user_id, ev)
            payload = _config_payload(s, ev)
            if (getattr(ev, "mode", None) or "standard") == "clan_vs_clan":
                payload["groups"] = _group_scopes(s, ev)
                payload["my_group_ids"] = sorted(_admin_participating_group_ids(s, user_id, ev))
                payload["is_host_admin"] = _is_host_admin(s, user_id, ev)
            return payload

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_discord_bp.put("/events/<int:event_id>/discord")
async def put_event_discord(event_id: int):
    """Replace the event's Discord destination: `{ guild_id, channels }`.

    `guild_id: null` clears the whole config. Channel ids are validated
    against the bot's channel cache for the chosen guild *when it's warm*
    (a cold cache never blocks saving — manual-id entry must always work)."""
    user_id = current_user_id()
    body = await json_body()

    # Scope: absent/None = the shared/host config; a group id = that clan's
    # own per-group destinations (web48a).
    scope_group_id = body.get("group_id")
    if scope_group_id is not None and (
            not isinstance(scope_group_id, int) or isinstance(scope_group_id, bool)):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer or null.")

    guild_id = body.get("guild_id")
    raw_channels = body.get("channels") or {}
    if not isinstance(raw_channels, dict):
        abort_problem(422, "Invalid channels", "'channels' must be an object of kind -> channel id.")

    # Both optional for backward compatibility: absent keys leave the stored
    # value unchanged (an explicit `pings: {}` clears the ping config).
    discord_event_policy = body.get("discord_event_policy")
    if discord_event_policy is not None and discord_event_policy not in EVENT_DISCORD_POLICIES:
        abort_problem(
            422, "Invalid Discord event policy",
            f"discord_event_policy must be one of {list(EVENT_DISCORD_POLICIES)}.",
        )
    pings_provided = "pings" in body
    ping_config = _parse_ping_config(body.get("pings")) if pings_provided else None
    messages_provided = "messages" in body
    message_config = _parse_message_config(body.get("messages")) if messages_provided else None
    per_group_provided = "per_group_discord" in body
    per_group_value = body.get("per_group_discord")
    if per_group_provided and not isinstance(per_group_value, bool):
        abort_problem(422, "Invalid per_group_discord",
                      "'per_group_discord' must be a boolean.")
    if scope_group_id is not None and (discord_event_policy is not None
                                       or pings_provided or per_group_provided):
        abort_problem(422, "Event-level fields in group scope",
                      "discord_event_policy / pings / per_group_discord are "
                      "event-level — save them without group_id.")

    if guild_id is None:
        if raw_channels:
            abort_problem(422, "Invalid config", "Channels can't be set without a guild.")
        channels = {}
    else:
        guild_id = _clean_snowflake(guild_id, "guild_id")
        channels = {}
        for kind, channel_id in raw_channels.items():
            if kind not in EVENT_CHANNEL_KINDS:
                abort_problem(
                    422, "Invalid channel kind",
                    f"'{kind}' is not one of {list(EVENT_CHANNEL_KINDS)}.",
                )
            if channel_id in (None, ""):
                continue  # unset this kind
            channels[kind] = _clean_snowflake(channel_id, f"channels.{kind}")

        # Validate channel membership against the bot cache when it's warm.
        cached = _guild_channels(guild_id)
        if cached is not None:
            known = {str(c.get("id")): c for c in cached}
            for kind, channel_id in channels.items():
                if channel_id not in known:
                    # Could be a typo/foreign channel — or one created moments
                    # ago that the bot's cache hasn't caught up with. Ask the
                    # bot to re-fetch (drained within ~15s) so a retry works.
                    _request_channel_refresh(guild_id)
                    abort_problem(
                        422, "Channel not in guild",
                        f"Channel {channel_id} ({kind}) isn't in the bot's list "
                        f"of this server's channels. If you just created it, "
                        "wait a moment and save again; otherwise check the id.",
                    )
                # A forum itself isn't messageable — only its threads are.
                if known[channel_id].get("type") == "forum":
                    abort_problem(
                        422, "Forum channel selected",
                        f"Channel {channel_id} ({kind}) is a forum — pick one of "
                        "its threads instead.",
                    )
        else:
            _request_channel_refresh(guild_id)

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            if scope_group_id is not None:
                _assert_group_scope(s, user_id, ev, scope_group_id)
            else:
                _assert_event_admin(s, user_id, ev)
                if per_group_provided and not _is_host_admin(s, user_id, ev):
                    abort_problem(403, "Host admins only",
                                  "Only the host clan's admins can toggle "
                                  "per-group Discord config.")
                if per_group_provided and per_group_value and (
                        (getattr(ev, "mode", None) or "standard") != "clan_vs_clan"):
                    abort_problem(409, "Not clan-vs-clan",
                                  "Per-group Discord config only applies to "
                                  "clan-vs-clan events.")

            before = _config_payload(s, ev, group_id=scope_group_id)
            # The security boundary: only require guild authority when the
            # target is being set to a NEW guild. Keeping an already-vetted
            # (or superadmin-set) guild while editing channels stays open to
            # co-admins; pointing the event at a different guild does not.
            part = None
            if scope_group_id is not None:
                part = (s.query(EventGroup)
                        .filter(EventGroup.event_id == ev.id,
                                EventGroup.group_id == scope_group_id)
                        .first())
                current_guild = (str(part.discord_guild_id)
                                 if part and part.discord_guild_id else None)
            else:
                current_guild = str(ev.discord_guild_id) if ev.discord_guild_id else None
            if guild_id is not None and str(guild_id) != current_guild:
                _assert_can_target_guild(s, user_id, guild_id)

            if scope_group_id is not None:
                if part is None:  # accepted participant always has a row, but be safe
                    abort_problem(404, "Not a participant",
                                  f"Group {scope_group_id} has no participant row.")
                part.discord_guild_id = guild_id
                if messages_provided:
                    part.message_config = message_config
            else:
                ev.discord_guild_id = guild_id
                if discord_event_policy is not None:
                    ev.discord_event_policy = discord_event_policy
                if pings_provided:
                    ev.ping_config = ping_config
                if messages_provided:
                    ev.message_config = message_config
                if per_group_provided:
                    ev.per_group_discord = per_group_value

            existing = {
                row.kind: row
                for row in s.query(EventChannel)
                .filter(EventChannel.event_id == event_id)
                .all()
                if (getattr(row, "group_id", None) or None) == scope_group_id
            }
            for kind, row in existing.items():
                if kind not in channels:
                    s.delete(row)
            for kind, channel_id in channels.items():
                if kind in existing:
                    row = existing[kind]
                    if str(row.channel_id) != str(channel_id):
                        # Re-pointed channel: the persistent message (the live
                        # standings board) lives in the OLD channel — forget
                        # it so the bot posts fresh in the new one.
                        row.message_id = None
                        row.message_updated_at = None
                    row.channel_id = channel_id
                else:
                    s.add(EventChannel(event_id=event_id, kind=kind,
                                       channel_id=channel_id,
                                       group_id=scope_group_id))
            if scope_group_id is None:
                # Keep the Discord scheduled-event mirror in step: a re-pointed
                # guild retires the old guild's row (delete_pending) and seeds
                # the new one (pending); clearing the guild retires everything.
                _sync_event_guilds(s, ev)
            # Per-team Discord rows follow the (possibly re-pointed) guild in
            # either scope (web53a; no-op when the feature is off).
            try:
                from services.event_team_discord import sync_event_team_discord

                sync_event_team_discord(s, ev)
            except ImportError:  # unit-test stubs
                pass
            s.flush()

            after = _config_payload(s, ev, group_id=scope_group_id)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=scope_group_id if scope_group_id is not None else ev.group_id,
                event_id=event_id,
                action="event.discord.update",
                target=f"web_events.{event_id}" + (
                    f".group.{scope_group_id}" if scope_group_id is not None else ""),
                before=json.dumps({
                    "guild_id": before["guild_id"], "channels": before["channels"],
                    "discord_event_policy": before["discord_event_policy"],
                    "pings": before["pings"], "messages": before["messages"],
                    "per_group_discord": before["per_group_discord"],
                }),
                after=json.dumps({
                    "guild_id": after["guild_id"], "channels": after["channels"],
                    "discord_event_policy": after["discord_event_policy"],
                    "pings": after["pings"], "messages": after["messages"],
                    "per_group_discord": after["per_group_discord"],
                }),
            ))
            s.commit()
            return after

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Per-team Discord channels & roles (web53a)
# --------------------------------------------------------------------------- #
def _team_discord_scope_row(s, ev: Event, scope_group_id):
    """(config_owner, guild_id) for one scope: the Event itself (shared/host)
    or the clan's EventGroup participant row."""
    if scope_group_id is None:
        return ev, (str(ev.discord_guild_id) if ev.discord_guild_id else None)
    part = (s.query(EventGroup)
            .filter(EventGroup.event_id == ev.id,
                    EventGroup.group_id == scope_group_id)
            .first())
    if part is None:
        abort_problem(404, "Not a participant",
                      f"Group {scope_group_id} has no participant row.")
    return part, (str(part.discord_guild_id) if part.discord_guild_id else None)


def _explicit_team_keys(config: dict, team_id) -> dict:
    """Which notification knobs a team has explicitly set (vs inherited):
    ``{"toggles": [keys], "pings": [keys], "task_progress": bool}``."""
    entry = (config.get("teams") or {}).get(str(team_id)) or {}
    return {
        "toggles": sorted((entry.get("toggles") or {}).keys()),
        "pings": sorted((entry.get("pings") or {}).keys()),
        "task_progress": "task_progress" in entry,
    }


def _team_discord_payload(s, ev: Event, scope_group_id=None) -> dict:
    """One team-discord config scope + live per-team provisioning state."""
    from services.event_notifications import effective_message_config
    from services.event_team_discord import (
        DEFAULT_TEAM_MESSAGE_PINGS,
        effective_team_discord_config,
        inherited_team_defaults,
        team_flags,
        team_message_pings,
        team_message_toggles,
        team_task_progress_mode,
    )

    owner, guild_id = _team_discord_scope_row(s, ev, scope_group_id)
    config = effective_team_discord_config(getattr(owner, "team_discord_config", None))
    # Team defaults inherit the scope's configured verbosity (a clan's own
    # override in a per-group scope, else the event's) until a team explicitly
    # changes a knob.
    scope_messages = (getattr(owner, "message_config", None)
                      or getattr(ev, "message_config", None))
    inherited = inherited_team_defaults(effective_message_config(scope_messages))

    teams_q = s.query(EventTeam).filter(EventTeam.event_id == ev.id)
    if scope_group_id is not None:
        teams_q = teams_q.filter(EventTeam.group_id == scope_group_id)
    teams = teams_q.order_by(EventTeam.id.asc()).all()

    rows = {}
    if guild_id:
        for r in (s.query(EventTeamDiscord)
                  .filter(EventTeamDiscord.event_id == ev.id,
                          EventTeamDiscord.guild_id == guild_id)
                  .all()):
            rows[r.team_id] = r

    team_states = []
    for t in teams:
        flags = team_flags(config, t.id)
        row = rows.get(t.id)
        team_states.append({
            "team_id": t.id,
            "name": t.name,
            "role_enabled": flags["role"],
            "channel_enabled": flags["channel"],
            "toggles": team_message_toggles(config, t.id, inherited=inherited),
            "pings": team_message_pings(config, t.id),
            "task_progress": team_task_progress_mode(config, t.id,
                                                     inherited=inherited),
            # Which knobs this team has explicitly set (everything else is
            # inherited — the UI labels those "event default").
            "explicit": _explicit_team_keys(config, t.id),
            "role_id": str(row.role_id) if row and row.role_id else None,
            "channel_id": str(row.channel_id) if row and row.channel_id else None,
            "channel_kind": row.channel_kind if row else None,
            "sync_status": row.sync_status if row else None,
            # Surfaced whenever set — member-sync 403s (bot role below the
            # team role) leave the row "synced" but carry an actionable
            # last_error the leader must see (P0-11).
            "last_error": row.last_error if row else None,
        })
    return {
        "group_id": scope_group_id,
        "guild_id": guild_id,
        "channels_enabled": config["channels_enabled"],
        "roles_enabled": config["roles_enabled"],
        "forum_channel_id": config["forum_channel_id"],
        "category_channel_id": config["category_channel_id"],
        "retention": config["retention"],
        "captain_config": config["captain_config"],
        "teams": team_states,
        # The scope's inherited baseline — what an untouched team gets.
        "default_toggles": dict(inherited["toggles"]),
        "default_pings": dict(DEFAULT_TEAM_MESSAGE_PINGS),
        "default_task_progress": inherited["task_progress"],
    }


@event_discord_bp.get("/events/<int:event_id>/team-discord")
async def get_event_team_discord(event_id: int):
    """Per-team Discord provisioning config + live state for one scope
    (shared/host by default, a participating clan's own via ``?group_id=``)."""
    user_id = current_user_id()
    raw_group = (request.args.get("group_id") or "").strip()
    scope_group_id = int(raw_group) if raw_group.isdigit() else None

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            if scope_group_id is not None:
                _assert_group_scope(s, user_id, ev, scope_group_id)
            else:
                _assert_event_admin(s, user_id, ev)
            return _team_discord_payload(s, ev, scope_group_id)

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_discord_bp.put("/events/<int:event_id>/team-discord")
async def put_event_team_discord(event_id: int):
    """Replace one scope's team-discord config. Body mirrors the effective
    config shape: ``{group_id?, channels_enabled, roles_enabled,
    forum_channel_id, category_channel_id, retention, captain_config, teams:
    {"<team_id>": {"role", "channel"}}}``. ``forum_channel_id`` = threads (no
    per-thread perms); ``category_channel_id`` = per-team text channels inside
    that category (role-restricted, private). Saving immediately
    (re)materializes the desired ``web_event_team_discord`` rows — the bot
    provisions within ~30s."""
    from services.event_team_discord import (
        effective_team_discord_config,
        sync_event_team_discord,
    )

    user_id = current_user_id()
    body = await json_body()

    scope_group_id = body.get("group_id")
    if scope_group_id is not None and (
            not isinstance(scope_group_id, int) or isinstance(scope_group_id, bool)):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer or null.")

    for key in ("channels_enabled", "roles_enabled", "captain_config"):
        if key in body and not isinstance(body[key], bool):
            abort_problem(422, "Invalid config", f"'{key}' must be a boolean.")
    retention = body.get("retention")
    if retention is not None and retention not in EVENT_TEAM_DISCORD_RETENTIONS:
        abort_problem(422, "Invalid retention",
                      f"retention must be one of {list(EVENT_TEAM_DISCORD_RETENTIONS)}.")
    forum_channel_id = body.get("forum_channel_id")
    if forum_channel_id not in (None, ""):
        forum_channel_id = _clean_snowflake(forum_channel_id, "forum_channel_id")
    else:
        forum_channel_id = None
    category_channel_id = body.get("category_channel_id")
    if category_channel_id not in (None, ""):
        category_channel_id = _clean_snowflake(category_channel_id, "category_channel_id")
    else:
        category_channel_id = None
    raw_teams = body.get("teams")
    if raw_teams is not None and not isinstance(raw_teams, dict):
        abort_problem(422, "Invalid teams",
                      "'teams' must be an object of team id -> {role, channel}.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            if scope_group_id is not None:
                _assert_group_scope(s, user_id, ev, scope_group_id)
            else:
                _assert_event_admin(s, user_id, ev)
            owner, guild_id = _team_discord_scope_row(s, ev, scope_group_id)

            enabling = bool(body.get("channels_enabled") or body.get("roles_enabled"))
            if enabling and not guild_id:
                abort_problem(
                    422, "No Discord server",
                    "Point the event at a Discord server first (the Server & "
                    "channels section) — team roles and channels are created "
                    "inside it.")

            # Forum target must actually be a forum channel of that guild —
            # validated against the bot cache when it's warm (a cold cache
            # never blocks saving; the bot fails the row with a clear error
            # if the id turns out not to be a forum).
            if forum_channel_id and guild_id:
                cached = _guild_channels(guild_id)
                if cached is not None:
                    known = {str(c.get("id")): c for c in cached}
                    entry = known.get(forum_channel_id)
                    if entry is None:
                        _request_channel_refresh(guild_id)
                        abort_problem(
                            422, "Channel not in guild",
                            "The forum channel isn't in the bot's list of this "
                            "server's channels. If you just created it, wait a "
                            "moment and save again; otherwise check the id.")
                    if entry.get("type") != "forum":
                        abort_problem(
                            422, "Not a forum channel",
                            "Team threads need a FORUM channel target — pick a "
                            "forum, or leave it empty to create text channels.")
                else:
                    _request_channel_refresh(guild_id)

            # Category target must actually be a CATEGORY channel of that guild
            # (same warm-cache-only validation as the forum target above).
            if category_channel_id and guild_id:
                cached = _guild_channels(guild_id)
                if cached is not None:
                    known = {str(c.get("id")): c for c in cached}
                    entry = known.get(category_channel_id)
                    if entry is None:
                        _request_channel_refresh(guild_id)
                        abort_problem(
                            422, "Channel not in guild",
                            "The category isn't in the bot's list of this "
                            "server's channels. If you just created it, wait a "
                            "moment and save again; otherwise check the id.")
                    if entry.get("type") != "category":
                        abort_problem(
                            422, "Not a category",
                            "Per-team channels need a CATEGORY target — pick a "
                            "channel category, or leave it empty to create "
                            "channels at the server root.")
                else:
                    _request_channel_refresh(guild_id)

            # Only known team ids, only this scope's teams (clan scopes may
            # not toggle another clan's teams).
            valid_q = s.query(EventTeam.id).filter(EventTeam.event_id == ev.id)
            if scope_group_id is not None:
                valid_q = valid_q.filter(EventTeam.group_id == scope_group_id)
            valid_ids = {str(tid) for (tid,) in valid_q.all()}
            teams_clean = {}
            for tid, entry in (raw_teams or {}).items():
                if str(tid) not in valid_ids:
                    abort_problem(422, "Unknown team",
                                  f"Team {tid} is not part of this scope.")
                if not isinstance(entry, dict):
                    abort_problem(422, "Invalid teams",
                                  f"teams.{tid} must be an object.")
                clean = {}
                for key in ("role", "channel"):
                    if key in entry:
                        if not isinstance(entry[key], bool):
                            abort_problem(422, "Invalid teams",
                                          f"teams.{tid}.{key} must be a boolean.")
                        clean[key] = entry[key]
                if clean:
                    teams_clean[str(tid)] = clean

            before = _team_discord_payload(s, ev, scope_group_id)

            # Merge onto the current stored config: absent top-level keys keep
            # their value; per-team entries merge key-wise (captain-saved
            # notification toggles survive an admin save and vice versa).
            current = effective_team_discord_config(
                getattr(owner, "team_discord_config", None))
            merged = dict(current)
            for key in ("channels_enabled", "roles_enabled", "captain_config"):
                if key in body:
                    merged[key] = bool(body[key])
            if "forum_channel_id" in body:
                merged["forum_channel_id"] = forum_channel_id
            if "category_channel_id" in body:
                merged["category_channel_id"] = category_channel_id
            if retention is not None:
                merged["retention"] = retention
            merged_teams = {k: dict(v) for k, v in current.get("teams", {}).items()}
            for tid, entry in teams_clean.items():
                merged_teams.setdefault(tid, {}).update(entry)
            merged["teams"] = merged_teams
            owner.team_discord_config = json.dumps(merged)

            sync_event_team_discord(s, ev)
            s.flush()
            after = _team_discord_payload(s, ev, scope_group_id)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=scope_group_id if scope_group_id is not None else ev.group_id,
                event_id=event_id,
                action="event.team_discord.update",
                target=f"web_events.{event_id}" + (
                    f".group.{scope_group_id}" if scope_group_id is not None else ""),
                before=json.dumps({k: before[k] for k in (
                    "channels_enabled", "roles_enabled", "forum_channel_id",
                    "category_channel_id", "retention", "captain_config")}),
                after=json.dumps({k: after[k] for k in (
                    "channels_enabled", "roles_enabled", "forum_channel_id",
                    "category_channel_id", "retention", "captain_config")}),
            ))
            s.commit()
            return after

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))


def _is_team_captain(s, user_id: int, event_id: int, team_id: int) -> bool:
    """Whether the user owns a player holding leader/co_leader on this team."""
    row = (s.query(EventTeamMember.player_id)
           .join(Player, Player.player_id == EventTeamMember.player_id)
           .filter(EventTeamMember.team_id == team_id,
                   EventTeamMember.role.in_(EVENT_TEAM_ROLES),
                   Player.user_id == user_id)
           .first())
    return row is not None


def _team_notification_owners(s, ev: Event, team) -> list:
    """Config owners covering this team: the event itself, plus (clan-vs-clan)
    the clan's own participant row when it runs its own team-discord scope.
    Captain edits write to every owner so the choice applies wherever the
    team's channel lives; reads prefer the most specific (last)."""
    owners = [_team_discord_scope_row(s, ev, None)[0]]
    if team.group_id:
        part = (s.query(EventGroup)
                .filter(EventGroup.event_id == ev.id,
                        EventGroup.group_id == team.group_id,
                        EventGroup.team_discord_config.isnot(None))
                .first())
        if part is not None:
            owners.append(part)
    return owners


def _assert_team_notifications_access(s, user_id: int, ev: Event, team,
                                      owners: list) -> None:
    """Event admins always; otherwise the team's captain, when leadership is
    on and no covering scope disabled captain_config."""
    from services.event_team_discord import effective_team_discord_config

    try:
        _assert_event_admin(s, user_id, ev)
        return
    except Exception:
        pass
    from services.event_leadership import effective_leadership

    leadership = effective_leadership(getattr(ev, "leadership_config", None))
    if not leadership.get("enabled"):
        abort_problem(403, "Admins only",
                      "Team leadership is disabled on this event — "
                      "only event admins can tune team notifications.")
    captain_allowed = any(
        effective_team_discord_config(
            getattr(o, "team_discord_config", None)
        ).get("captain_config", True)
        for o in owners
    )
    if not captain_allowed:
        abort_problem(403, "Admins only",
                      "Captain configuration is disabled for this event.")
    if not _is_team_captain(s, user_id, ev.id, team.id):
        abort_problem(403, "Not your team",
                      "Only this team's leader or co-leader can tune "
                      "its notifications.")


def _team_notifications_state(ev: Event, team_id: int, owners: list) -> dict:
    """Effective notification state for one team's channel — inherited scope
    baseline overlaid with the team's explicit choices, plus which knobs are
    explicit (the UI labels the rest "event default")."""
    from services.event_notifications import effective_message_config
    from services.event_team_discord import (
        effective_team_discord_config,
        inherited_team_defaults,
        team_message_pings,
        team_message_toggles,
        team_task_progress_mode,
    )

    owner = owners[-1]  # most specific scope (the clan's own, when present)
    config = effective_team_discord_config(
        getattr(owner, "team_discord_config", None))
    scope_messages = (getattr(owner, "message_config", None)
                      or getattr(ev, "message_config", None))
    inherited = inherited_team_defaults(effective_message_config(scope_messages))
    return {
        "team_id": team_id,
        "toggles": team_message_toggles(config, team_id, inherited=inherited),
        "pings": team_message_pings(config, team_id),
        "task_progress": team_task_progress_mode(config, team_id,
                                                 inherited=inherited),
        "explicit": _explicit_team_keys(config, team_id),
        "inherited": inherited,
    }


@event_discord_bp.get("/events/<int:event_id>/teams/<int:team_id>/notifications")
async def get_team_notifications(event_id: int, team_id: int):
    """Current effective notification state for one team's channel — what the
    captain modal seeds from (same access rules as the PUT)."""
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            team = (s.query(EventTeam)
                    .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                    .first())
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")
            owners = _team_notification_owners(s, ev, team)
            _assert_team_notifications_access(s, user_id, ev, team, owners)
            return _team_notifications_state(ev, team_id, owners)

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


@event_discord_bp.put("/events/<int:event_id>/teams/<int:team_id>/notifications")
async def put_team_notifications(event_id: int, team_id: int):
    """A team captain (leadership feature, when the scope allows
    ``captain_config``) — or any event admin — tunes the team's channel:
    ``{toggles: {type: bool}, pings: {type: bool}, task_progress:
    'off'|'milestones'|'all'}``. ``toggles`` = post it at all; ``pings`` =
    mention @TeamRole when it posts. Saved into every config scope that covers
    the team, so the choice applies wherever the channel lives. Untouched
    knobs keep inheriting the event's configured verbosity."""
    from services.event_team_discord import (
        DEFAULT_TEAM_MESSAGE_PINGS,
        DEFAULT_TEAM_MESSAGE_TOGGLES,
        effective_team_discord_config,
    )

    user_id = current_user_id()
    body = await json_body()

    toggles = body.get("toggles")
    if toggles is not None:
        if not isinstance(toggles, dict):
            abort_problem(422, "Invalid toggles", "'toggles' must be an object.")
        for key, val in toggles.items():
            if key not in DEFAULT_TEAM_MESSAGE_TOGGLES:
                abort_problem(422, "Unknown toggle",
                              f"'{key}' is not one of "
                              f"{sorted(DEFAULT_TEAM_MESSAGE_TOGGLES)}.")
            if not isinstance(val, bool):
                abort_problem(422, "Invalid toggle", f"toggles.{key} must be a boolean.")
    pings = body.get("pings")
    if pings is not None:
        if not isinstance(pings, dict):
            abort_problem(422, "Invalid pings", "'pings' must be an object.")
        for key, val in pings.items():
            if key not in DEFAULT_TEAM_MESSAGE_PINGS:
                abort_problem(422, "Unknown ping toggle",
                              f"'{key}' is not one of "
                              f"{sorted(DEFAULT_TEAM_MESSAGE_PINGS)}.")
            if not isinstance(val, bool):
                abort_problem(422, "Invalid ping toggle",
                              f"pings.{key} must be a boolean.")
    task_progress = body.get("task_progress")
    if task_progress is not None and task_progress not in EVENT_TASK_PROGRESS_MODES:
        abort_problem(422, "Invalid task progress mode",
                      f"task_progress must be one of {list(EVENT_TASK_PROGRESS_MODES)}.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            team = (s.query(EventTeam)
                    .filter(EventTeam.id == team_id, EventTeam.event_id == event_id)
                    .first())
            if not team:
                abort_problem(404, "Team not found", f"No team {team_id} in this event.")

            owners = _team_notification_owners(s, ev, team)
            _assert_team_notifications_access(s, user_id, ev, team, owners)

            for owner in owners:
                current = effective_team_discord_config(
                    getattr(owner, "team_discord_config", None))
                teams_cfg = {k: dict(v) for k, v in current.get("teams", {}).items()}
                entry = teams_cfg.setdefault(str(team_id), {})
                if toggles is not None:
                    merged_toggles = dict(entry.get("toggles") or {})
                    merged_toggles.update(toggles)
                    entry["toggles"] = merged_toggles
                if pings is not None:
                    merged_pings = dict(entry.get("pings") or {})
                    merged_pings.update(pings)
                    entry["pings"] = merged_pings
                if task_progress is not None:
                    entry["task_progress"] = task_progress
                current["teams"] = teams_cfg
                owner.team_discord_config = json.dumps(current)

            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                event_id=event_id,
                action="event.team_discord.notifications",
                target=f"web_events.{event_id}.team.{team_id}",
                before=None,
                after=json.dumps({"toggles": toggles, "pings": pings,
                                  "task_progress": task_progress}),
            ))
            s.commit()
            return _team_notifications_state(ev, team_id, owners)

    payload = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(payload))
