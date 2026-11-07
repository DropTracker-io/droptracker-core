"""Join-to-create voice rooms (suggestion #237): the Discord shell.

Joining a group's hub channel (``vc_join_to_create``) creates a temporary
voice channel in the hub's category and moves the member into it; the room
is deleted when the last person leaves. Pure rules and the Redis record
layout are in ``services/voice_rooms_core.py``.

Needs the GUILD_VOICE_STATES intent (``bots/main.py``): the room's occupancy
is read from the library's voice-state cache, which the gateway keeps current
and rebuilds from GUILD_CREATE after a restart. Events for channels that are
neither a hub nor a room return before touching the database or Redis, so
voice traffic in the other few hundred guilds costs a set lookup each.

The bot needs Manage Channels and Move Members where the hub is. Without
them the member simply stays in the hub; the failure is logged per guild,
at most once an hour, rather than on every join.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

import interactions
from interactions import Extension, IntervalTrigger, Task, listen
from interactions.api.events import VoiceStateUpdate

from services import voice_rooms_core as core

log = logging.getLogger("voice_rooms")

_VOICE = 2
_HUB_REFRESH_S = 60
_FAIL_LOG_EVERY_S = 3600


def _redis():
    from utils.redis import redis_client

    return redis_client.client


def _load_hub_rows():
    from db.models import GroupConfiguration, Session

    s = Session()
    try:
        return [(gid, val or long_val) for gid, val, long_val in
                s.query(GroupConfiguration.group_id, GroupConfiguration.config_value,
                        GroupConfiguration.long_value)
                .filter(GroupConfiguration.config_key == core.HUB_KEY).all()]
    finally:
        s.close()


def _load_name_template(group_id: int) -> Optional[str]:
    from db.models import Session
    from utils import group_config as gc

    s = Session()
    try:
        return gc.get_bulk(s, [group_id], [core.NAME_KEY]).get((group_id, core.NAME_KEY))
    finally:
        s.close()


class VoiceRooms(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        self._hubs: Dict[int, int] = {}
        self._hubs_at = 0.0
        self._rooms: set = set()
        self._cooldown: Dict[tuple, float] = {}
        self._failed_at: Dict[int, float] = {}
        self._explained_at: Dict[int, float] = {}
        try:
            self._rooms = {int(k) for k in (_redis().hkeys(core.ROOMS_HASH) or [])}
        except Exception:
            log.warning("[VoiceRooms] couldn't load rooms from Redis", exc_info=True)
        self.sweep.start()

    # -- lookups -------------------------------------------------------------
    async def _hub_group(self, channel_id: int) -> Optional[int]:
        if time.monotonic() - self._hubs_at > _HUB_REFRESH_S:
            self._hubs_at = time.monotonic()
            try:
                rows = await asyncio.to_thread(_load_hub_rows)
                self._hubs = core.hub_map(rows)
            except Exception:
                log.warning("[VoiceRooms] hub refresh failed", exc_info=True)
        return self._hubs.get(int(channel_id))

    def _occupancy(self, channel_id: int) -> Optional[int]:
        """Members in a channel per the voice-state cache; None if uncached."""
        channel = self.bot.cache.get_channel(channel_id)
        if channel is None:
            return None
        return len(getattr(channel, "_voice_member_ids", None) or [])

    def _note_failure(self, guild_id, what: str) -> None:
        now = time.monotonic()
        if now - self._failed_at.get(int(guild_id), 0) > _FAIL_LOG_EVERY_S:
            self._failed_at[int(guild_id)] = now
            log.warning("[VoiceRooms] guild %s: %s (needs Manage Channels + Move Members)",
                        guild_id, what, exc_info=True)

    # -- gateway -------------------------------------------------------------
    @listen(VoiceStateUpdate)
    async def on_voice_state(self, event: VoiceStateUpdate) -> None:
        try:
            before, after = event.before, event.after
            left = int(before._channel_id) if before and before._channel_id else None
            joined = int(after._channel_id) if after and after._channel_id else None
            if left == joined:
                return  # mute / deafen / stream toggles
            if left is not None and left in self._rooms:
                await self._maybe_delete(left)
            if joined is not None and after._guild_id:
                group_id = await self._hub_group(joined)
                if group_id is not None:
                    await self._open_room(after, joined, group_id)
        except Exception:
            log.exception("[VoiceRooms] voice state handling failed")

    async def _open_room(self, state, hub_id: int, group_id: int) -> None:
        guild_id = int(state._guild_id)
        user_id = int(state.user_id)
        r = _redis()
        field = core.owner_field(guild_id, user_id)

        # Already has a live room here: send them back to it.
        existing = r.hget(core.OWNERS_HASH, field)
        if existing is not None and int(existing) in self._rooms:
            if await self._move(guild_id, user_id, int(existing)):
                return
            await self._forget(int(existing))

        key = (guild_id, user_id)
        now = time.monotonic()
        if now - self._cooldown.get(key, 0) < core.CREATE_COOLDOWN_S:
            return
        self._cooldown[key] = now

        in_guild = sum(1 for raw in r.hvals(core.ROOMS_HASH)
                       if (core.parse_record(raw) or {}).get("guild_id") == str(guild_id))
        if in_guild >= core.MAX_ROOMS_PER_GUILD:
            return

        hub = self.bot.cache.get_channel(hub_id)
        member = state.member
        display = (getattr(member, "display_name", None) if member else None) or "Member"
        template = await asyncio.to_thread(_load_name_template, group_id)
        name = core.render_room_name(template, display)
        parent_id = getattr(hub, "parent_id", None) if hub else None
        bitrate = int(getattr(hub, "bitrate", 0) or 64000) if hub else 64000
        try:
            # No permission_overwrites: Discord syncs the room to its
            # category, so a private category stays private.
            data = await self.bot.http.create_guild_channel(
                guild_id, name, _VOICE, parent_id=parent_id, bitrate=bitrate,
                reason=f"Voice room for {display}")
        except Exception:
            self._note_failure(guild_id, "couldn't create a voice room")
            await self._explain(guild_id, hub_id, parent_id, user_id)
            return
        room_id = int(data["id"])
        self._rooms.add(room_id)
        r.hset(core.ROOMS_HASH, str(room_id), core.room_record(guild_id, user_id, hub_id))
        r.hset(core.OWNERS_HASH, field, str(room_id))
        if not await self._move(guild_id, user_id, room_id):
            # They left the hub before we could move them, or we lack Move
            # Members: an empty room is no use to anyone.
            await self._delete(room_id, "Voice room owner left before moving in")
            if self._occupancy(hub_id):
                await self._explain(guild_id, hub_id, parent_id, user_id)

    async def _explain(self, guild_id: int, hub_id: int, parent_id, user_id: int) -> None:
        """Tell the server what permission is missing, at most once an hour.

        Discord only says "Missing Permissions", and the member who joined the
        hub otherwise sees nothing happen. Said in the hub's own text chat
        (every voice channel has one), or by DM to the member if the bot
        can't post there.
        """
        now = time.monotonic()
        if now - self._explained_at.get(int(guild_id), 0) < _FAIL_LOG_EVERY_S:
            return
        self._explained_at[int(guild_id)] = now
        try:
            roles = {str(r["id"]): int(r["permissions"])
                     for r in await self.bot.http.get_roles(guild_id)}
            me = await self.bot.http.get_member(guild_id, self.bot.user.id)
            where = await self.bot.http.get_channel(parent_id or hub_id)
            overwrites = where.get("permission_overwrites") or []
            perms = core.effective_permissions(guild_id, self.bot.user.id,
                                               me.get("roles"), roles, overwrites)
            missing = core.missing_room_permissions(perms, overwrites)
            name = where.get("name") if parent_id else None
        except Exception:
            log.warning("[VoiceRooms] permission check failed in %s", guild_id, exc_info=True)
            return
        if not missing:
            return
        text = core.permissions_notice(missing, name)
        payload = {"content": text, "allowed_mentions": {"parse": []}}
        try:
            await self.bot.http.create_message(payload, hub_id)
            return
        except Exception:
            pass
        try:
            user = await self.bot.fetch_user(user_id)
            if user is not None:
                await user.send(text)
        except Exception:
            pass

    async def _move(self, guild_id: int, user_id: int, room_id: int) -> bool:
        try:
            await self.bot.http.modify_guild_member(guild_id, user_id, channel_id=room_id,
                                                    reason="Join-to-create voice room")
            return True
        except Exception:
            self._note_failure(guild_id, "couldn't move a member into their voice room")
            return False

    async def _maybe_delete(self, room_id: int) -> None:
        # The library updates occupancy before dispatching, so 0 is current.
        if self._occupancy(room_id) == 0:
            await self._delete(room_id, "Voice room is empty")

    async def _delete(self, room_id: int, reason: str) -> None:
        try:
            await self.bot.http.delete_channel(room_id, reason=reason)
        except Exception as e:
            status = getattr(e, "status", None)
            if status not in (404,):
                log.warning("[VoiceRooms] delete %s failed: %s", room_id, e)
                if status != 403:
                    return  # transient: the sweep retries
        await self._forget(room_id)

    async def _forget(self, room_id: int) -> None:
        self._rooms.discard(room_id)
        r = _redis()
        rec = core.parse_record(r.hget(core.ROOMS_HASH, str(room_id)))
        r.hdel(core.ROOMS_HASH, str(room_id))
        if rec:
            field = core.owner_field(rec["guild_id"], rec["owner_id"])
            if r.hget(core.OWNERS_HASH, field) in (str(room_id).encode(), str(room_id)):
                r.hdel(core.OWNERS_HASH, field)

    # -- sweep ---------------------------------------------------------------
    @Task.create(IntervalTrigger(minutes=2))
    async def sweep(self) -> None:
        """Delete empty rooms the event path missed (restarts, a failed
        delete, a member who left while the bot was offline)."""
        try:
            r = _redis()
            for raw_id, raw in (r.hgetall(core.ROOMS_HASH) or {}).items():
                room_id = int(raw_id)
                self._rooms.add(room_id)
                rec = core.parse_record(raw) or {}
                count = self._occupancy(room_id)
                if count is None:
                    # Not in the gateway cache: deleted by someone, or a guild
                    # that hasn't loaded yet. A REST copy carries no voice
                    # members, so it can only prove the room is gone, never
                    # that it is empty; an existing room waits for the cache.
                    try:
                        found = await self.bot.fetch_channel(room_id, force=True)
                    except Exception:
                        continue
                    if found is None:
                        await self._forget(room_id)
                    continue
                if core.sweepable(rec, count):
                    await self._delete(room_id, "Voice room is empty")
        except Exception:
            log.exception("[VoiceRooms] sweep failed")


def setup(bot) -> None:
    VoiceRooms(bot)
