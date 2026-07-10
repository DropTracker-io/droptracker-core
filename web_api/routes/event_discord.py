"""Task 19 — per-event Discord destinations (events-prd.md D8).

  GET /api/v1/events/{id}/discord                      -> EventChannelConfig
  PUT /api/v1/events/{id}/discord                      -> EventChannelConfig
  GET /api/v1/events/discord/guilds                    -> { guilds, stale }
  GET /api/v1/events/discord/guilds/{gid}/channels     -> { channels, stale }

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

from quart import Blueprint, jsonify

from db import (
    AuditLog,
    Event,
    EventChannel,
    Group,
    GroupAdmin,
    EVENT_CHANNEL_KINDS,
)
from web_api.common import abort_problem, db_session, private_no_store, _rc
from web_api.deps import (
    current_user_id,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
    resolve_group_role,
)
from web_api.routes.events import _assert_event_admin, _bump, _load_event_or_404

event_discord_bp = Blueprint("v1_event_discord", __name__)

# Bot-maintained caches (bots/main.py). Module-level so the verification
# script can rebind them to throwaway test keys.
_BOT_GUILDS_KEY = "bot:guilds"
_CHANNEL_REFRESH_KEY = "bot:channels:refresh"


def _channels_key(guild_id: str) -> str:
    return f"guild:{guild_id}:channels"


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
    # Add the home guilds of groups the user has an explicit web grant on
    # (bounded to this user's grants — no full-table scan).
    grant_group_ids = [
        gid for (gid,) in s.query(GroupAdmin.group_id)
        .filter(GroupAdmin.user_id == user_id).all()
    ]
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


def _config_payload(s, ev: Event) -> dict:
    rows = s.query(EventChannel).filter(EventChannel.event_id == ev.id).all()
    channels = {r.kind: str(r.channel_id) for r in rows if r.channel_id}
    guild_id = str(ev.discord_guild_id) if ev.discord_guild_id else None
    guild_name = None
    if guild_id:
        for g in _bot_guilds() or []:
            if str(g.get("id")) == guild_id:
                guild_name = g.get("name")
                break
    return {"guild_id": guild_id, "guild_name": guild_name, "channels": channels}


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
        if channels is None:
            # Cold cache: hand the UI its manual-id fallback and ask the bot
            # to warm this guild so a retry succeeds shortly.
            _request_channel_refresh(guild_id)
            return {"channels": [], "stale": True}
        return {"channels": channels, "stale": False}

    payload = await asyncio.to_thread(_load)
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# Per-event config
# --------------------------------------------------------------------------- #
@event_discord_bp.get("/events/<int:event_id>/discord")
async def get_event_discord(event_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)
            return _config_payload(s, ev)

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

    guild_id = body.get("guild_id")
    raw_channels = body.get("channels") or {}
    if not isinstance(raw_channels, dict):
        abort_problem(422, "Invalid channels", "'channels' must be an object of kind -> channel id.")

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
            known = {str(c.get("id")) for c in cached}
            for kind, channel_id in channels.items():
                if channel_id not in known:
                    abort_problem(
                        422, "Channel not in guild",
                        f"Channel {channel_id} ({kind}) doesn't belong to guild {guild_id}.",
                    )
        else:
            _request_channel_refresh(guild_id)

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev.group_id)

            before = _config_payload(s, ev)
            # The security boundary: only require guild authority when the
            # target is being set to a NEW guild. Keeping an already-vetted
            # (or superadmin-set) guild while editing channels stays open to
            # co-admins; pointing the event at a different guild does not.
            if guild_id is not None and str(guild_id) != (
                str(ev.discord_guild_id) if ev.discord_guild_id else None
            ):
                _assert_can_target_guild(s, user_id, guild_id)
            ev.discord_guild_id = guild_id

            existing = {
                row.kind: row
                for row in s.query(EventChannel).filter(EventChannel.event_id == event_id).all()
            }
            for kind, row in existing.items():
                if kind not in channels:
                    s.delete(row)
            for kind, channel_id in channels.items():
                if kind in existing:
                    existing[kind].channel_id = channel_id
                else:
                    s.add(EventChannel(event_id=event_id, kind=kind, channel_id=channel_id))
            s.flush()

            after = _config_payload(s, ev)
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                action="event.discord.update",
                target=f"web_events.{event_id}",
                before=json.dumps({"guild_id": before["guild_id"], "channels": before["channels"]}),
                after=json.dumps({"guild_id": after["guild_id"], "channels": after["channels"]}),
            ))
            s.commit()
            return after

    payload = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify(payload))
