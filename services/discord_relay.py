"""Discord delivery relay — the core bot's side (web153a).

Protocol and outbox live in :mod:`services.discord_relay_core`; this module
is the glue inside the core bot process:

* :func:`fetch_channel` — the drop-in for ``bot.fetch_channel`` at every send
  site a relay may take over. For a channel in a relay-owned server it
  returns a :class:`RelayChannel` whose ``send`` / ``fetch_message`` /
  ``message.edit`` / ``message.delete`` enqueue the exact request body the
  real channel would have sent (``interactions.process_message_payload``), so
  callers need no relay-specific code. Otherwise it is ``bot.fetch_channel``.
* :func:`run_interaction_consumer` — answers clicks the customer bots forward
  (sign-up flow, review buttons) with the SAME handlers our own buttons use,
  through the interaction token. No bot token of theirs is involved.
* :func:`run_resync_consumer` — the customer's "resync" command: forget any
  board message that is gone and redraw the group's live boards now.

Routing reads ``discord_relay_integrations`` (active rows only) through a
30-second in-process cache, dropped early whenever the web API bumps
``relay:routes:version``.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from services import discord_relay_core as core

log = logging.getLogger("discord_relay")

DISCORD_API = "https://discord.com/api/v10"
ROUTES_TTL_SECONDS = 30
_INTERACTION_MAX_AGE_SECONDS = 14 * 60  # tokens live 15 minutes


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #

def _redis():
    try:
        from utils.redis import RedisClient

        return RedisClient().client
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Route:
    integration_id: int
    group_id: int
    guild_id: str
    features: tuple
    hold_minutes: int
    application_id: Optional[str]


@dataclass
class _RouteCache:
    loaded_at: float = 0.0
    version: Optional[str] = None
    by_guild: dict = field(default_factory=dict)
    by_integration: dict = field(default_factory=dict)


_cache = _RouteCache()


def _load_routes() -> tuple:
    from db.models import DiscordRelayGuild, DiscordRelayIntegration, Session

    s = Session()
    try:
        rows = (
            s.query(DiscordRelayIntegration, DiscordRelayGuild.guild_id)
            .join(DiscordRelayGuild,
                  DiscordRelayGuild.integration_id == DiscordRelayIntegration.id)
            .filter(DiscordRelayIntegration.status == "active")
            .all()
        )
        by_guild, by_integration = {}, {}
        for integ, guild_id in rows:
            route = Route(
                integration_id=integ.id, group_id=integ.group_id, guild_id=str(guild_id),
                features=tuple(core.normalize_features(integ.features)),
                hold_minutes=int(integ.hold_minutes or 360),
                application_id=integ.application_id,
            )
            by_guild[str(guild_id)] = route
            by_integration.setdefault(integ.id, []).append(route)
        return by_guild, by_integration
    finally:
        s.close()


def routes(force: bool = False) -> dict:
    """guild_id → Route for every active integration (cached)."""
    now = time.time()
    version = None
    r = _redis()
    if r is not None:
        try:
            version = core._s(r.get(core.ROUTES_VERSION_KEY))
        except Exception:
            version = _cache.version
    fresh = now - _cache.loaded_at < ROUTES_TTL_SECONDS and version == _cache.version
    if force or not fresh:
        try:
            _cache.by_guild, _cache.by_integration = _load_routes()
        except Exception:
            log.exception("relay: route load failed; keeping the previous table")
        _cache.loaded_at, _cache.version = now, version
    return _cache.by_guild


def invalidate_routes() -> None:
    _cache.loaded_at = 0.0


def route_for_guild(guild_id, feature: str) -> Optional[Route]:
    if not guild_id:
        return None
    route = routes().get(str(guild_id))
    if route is None or feature not in route.features:
        return None
    return route


def guild_relayed(guild_id, feature: str = "events") -> bool:
    """For subsystems that act on a whole server (team channels/roles,
    Discord scheduled events): skip a server whose events a relay owns — the
    customer's bot is the only one meant to act there."""
    return route_for_guild(guild_id, feature) is not None


def relay_owned_guild_ids(session, feature: str = "events") -> set:
    """Guilds an ACTIVE integration owns ``feature`` for, read through the
    caller's session (no cache) — for the web API's desired-state writers
    (scheduled events, team channels/roles), which must not plan Discord work
    in a server where only the customer's bot acts."""
    from db.models import DiscordRelayGuild, DiscordRelayIntegration

    try:
        rows = (
            session.query(DiscordRelayGuild.guild_id, DiscordRelayIntegration.features)
            .join(DiscordRelayIntegration,
                  DiscordRelayIntegration.id == DiscordRelayGuild.integration_id)
            .filter(DiscordRelayIntegration.status == "active")
            .all()
        )
    except Exception:
        log.exception("relay: owned-guild lookup failed")
        return set()
    return {str(gid) for gid, feats in rows if feature in core.normalize_features(feats)}


# --------------------------------------------------------------------------- #
# DropTracker's own application emojis → fallbacks
# --------------------------------------------------------------------------- #

_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_~]+):(\d{15,22})>")
_foreign_emojis: Optional[dict] = None


def foreign_emojis() -> dict:
    """id → Unicode fallback ('' = drop) for every emoji DropTracker's apps own.

    Application emojis render only for the app that owns them, so on a
    message another bot sends they would show as raw ``<:name:id>`` text."""
    global _foreign_emojis
    if _foreign_emojis is not None:
        return _foreign_emojis
    out = {}
    try:
        from utils.app_emojis import SPECS
    except Exception:
        SPECS = {}
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
    for name in ("app_emojis.json", "game_emojis.json", "rank_emojis.json"):
        try:
            with open(os.path.join(base, name), encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            continue
        for emoji_name, emoji_id in _EMOJI_RE.findall(text):
            spec = SPECS.get(emoji_name)
            out[emoji_id] = getattr(spec, "fallback", "") or ""
    _foreign_emojis = out
    return out


# --------------------------------------------------------------------------- #
# The stand-ins
# --------------------------------------------------------------------------- #

def _file_bytes(f) -> Optional[tuple]:
    """``(filename, bytes)`` from anything ``channel.send(files=...)`` takes."""
    try:
        import interactions

        if isinstance(f, interactions.File):
            name = getattr(f, "file_name", None) or "file"
            src = f.file
        else:
            name, src = None, f
        if isinstance(src, (bytes, bytearray)):
            return name or "file", bytes(src)
        if isinstance(src, (str, os.PathLike)):
            with open(src, "rb") as fh:
                return name or os.path.basename(str(src)), fh.read()
        if isinstance(src, io.IOBase) or hasattr(src, "read"):
            try:
                src.seek(0)
            except Exception:
                pass
            return name or getattr(src, "name", "file"), src.read()
    except Exception:
        log.exception("relay: could not read an attachment")
    return None


def _files_list(files, file=None) -> list:
    items = []
    for f in ([file] if file is not None else []) + (
            list(files) if isinstance(files, (list, tuple)) else ([files] if files else [])):
        got = _file_bytes(f)
        if got:
            items.append(got)
    return items


def build_body(*, content=None, embeds=None, embed=None, components=None,
               allowed_mentions=None, files=None, attachments=None, flags=None,
               for_edit: bool = False) -> tuple:
    """``(body, files)`` — the Discord request body exactly as our bot would
    send it, rewritten for another application."""
    from interactions.models.discord.message import process_message_payload

    if embed is not None:
        embeds = ([embed] + list(embeds or [])) if embeds else [embed]
    body = process_message_payload(
        content=content, embeds=embeds, components=components,
        allowed_mentions=allowed_mentions, flags=flags,
    )
    file_items = _files_list(files)
    if file_items:
        # New uploads replace whatever the message carried (edit) — the
        # behaviour every caller wants (attachments=[] + files=new board).
        body["attachments"] = [{"id": i, "filename": name}
                               for i, (name, _data) in enumerate(file_items)]
    elif for_edit and attachments is not None and not attachments:
        body["attachments"] = []
    if for_edit and components is not None and "components" not in body:
        body["components"] = []  # an explicit "remove the buttons"
    return core.rewrite_body(body, foreign_emojis()), file_items


class RelayMessage:
    def __init__(self, channel: "RelayChannel", mid: int):
        self.channel = channel
        self.mid = int(mid)
        self.id = core.synthetic_id(mid)
        self.channel_id = channel.id
        self._guild_id = channel._guild_id

    async def edit(self, content=None, *, embeds=None, embed=None, components=None,
                   files=None, file=None, attachments=None, allowed_mentions=None,
                   **_ignored):
        body, file_items = build_body(
            content=content, embeds=embeds, embed=embed, components=components,
            files=([file] if file is not None else []) + (
                list(files) if isinstance(files, (list, tuple)) else ([files] if files else [])),
            attachments=attachments, allowed_mentions=allowed_mentions, for_edit=True)
        r = self.channel._r()
        await asyncio.to_thread(core.enqueue_edit, r, self.channel.route.integration_id,
                                self.mid, body, file_items, self.channel.ref)
        return self

    async def delete(self, *_a, **_k):
        r = self.channel._r()
        await asyncio.to_thread(core.enqueue_delete, r, self.channel.route.integration_id,
                                self.mid, self.channel.ref)


class RelayChannel:
    """Looks enough like an ``interactions`` text channel for our send sites."""

    type = 0  # GUILD_TEXT — never a thread, so thread upkeep stays out of it
    relayed = True

    def __init__(self, route: Route, channel_id, ref: Optional[str] = None):
        self.route = route
        self.id = int(channel_id)
        self._guild_id = int(route.guild_id)
        self.guild_id = self._guild_id
        self.ref = ref
        self.name = f"relay-{channel_id}"

    def _r(self):
        r = _redis()
        if r is None:
            raise RuntimeError("relay: redis unavailable")
        return r

    async def send(self, content=None, *, embeds=None, embed=None, components=None,
                   files=None, file=None, allowed_mentions=None, flags=None, **_ignored):
        body, file_items = build_body(
            content=content, embeds=embeds, embed=embed, components=components,
            files=([file] if file is not None else []) + (
                list(files) if isinstance(files, (list, tuple)) else ([files] if files else [])),
            allowed_mentions=allowed_mentions, flags=flags)
        mid = await asyncio.to_thread(core.enqueue_create, self._r(),
                                      self.route.integration_id, str(self.id),
                                      body, file_items, self.ref)
        return RelayMessage(self, mid)

    async def fetch_message(self, message_id=None, **_ignored):
        """Mirrors interactions: ``None`` when the message is gone, an
        exception only when we can't tell. Only messages this relay sent can
        be edited by the customer's bot, so anything else (gone, or posted by
        DropTracker's own bot before the relay was switched on) is ``None`` —
        and the caller reposts."""
        if message_id is None or not core.is_relay_message_id(message_id):
            return None
        mid = core.relay_mid(message_id)
        state = await asyncio.to_thread(core.target_state, self._r(), mid)
        if state != "live":
            return None
        return RelayMessage(self, mid)


async def fetch_channel(bot, channel_id, feature: str = "events", ref: Optional[str] = None):
    """``bot.fetch_channel`` that answers with a :class:`RelayChannel` for a
    server whose ``feature`` a relay integration owns.

    Guild resolution order: the relay's channel map (reported by the customer
    bot, or learned here), then our own bot's fetch. With no active
    integrations at all this is exactly ``bot.fetch_channel``.
    """
    table = routes()
    if not table:
        return await bot.fetch_channel(channel_id)
    r = _redis()
    guild_id = None
    if r is not None:
        try:
            guild_id = await asyncio.to_thread(core.channel_guild, r, channel_id)
        except Exception:
            guild_id = None
    real = None
    if guild_id is None:
        try:
            real = await bot.fetch_channel(channel_id)
        except Exception:
            real = None
        guild_id = getattr(real, "_guild_id", None)
        if guild_id and r is not None and str(guild_id) in table:
            try:
                await asyncio.to_thread(core.remember_channels, r, guild_id, [channel_id])
            except Exception:
                pass
    route = route_for_guild(guild_id, feature)
    if route is not None:
        return RelayChannel(route, channel_id, ref=ref)
    if real is None:
        real = await bot.fetch_channel(channel_id)
    return real


def is_relayed(channel) -> bool:
    return bool(getattr(channel, "relayed", False))


# --------------------------------------------------------------------------- #
# Interactions: answering forwarded clicks through the interaction token
# --------------------------------------------------------------------------- #

class DiscordHTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class InteractionResponder:
    """The three calls our handlers make, against Discord's token endpoints.

    Interaction callbacks and webhook-token routes authenticate by the token
    in the URL, so no Authorization header (and no customer bot token) is
    needed. The initial callback was already made by the customer's bot
    (the defer mode the data API told it), so :meth:`initial` is a no-op."""

    def __init__(self, application_id: str, token: str):
        self.application_id = str(application_id)
        self.token = token

    async def _request(self, method: str, path: str, payload: dict) -> Optional[dict]:
        import aiohttp

        url = f"{DISCORD_API}{path}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.request(method, url, json=payload,
                                    headers={"User-Agent": "DropTracker-Relay (https://www.droptracker.io, 1.0)"}) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise DiscordHTTPError(resp.status, f"Discord {method} -> {resp.status}: {text[:200]}")
                if resp.status == 204:
                    return None
                return await resp.json()

    async def initial(self, payload: dict) -> None:
        return None  # the customer bot made the initial callback

    async def _with_retry(self, method: str, path: str, payload: dict):
        # The customer bot defers and then forwards, but the two race: if our
        # reply beats its deferral to Discord, the token has no original yet
        # (404 Unknown Webhook/Message). Wait briefly instead of failing.
        delays = (0.5, 1.0, 2.0)
        for attempt in range(len(delays) + 1):
            try:
                return await self._request(method, path, payload)
            except DiscordHTTPError as exc:
                if exc.status != 404 or attempt == len(delays):
                    raise
                await asyncio.sleep(delays[attempt])

    async def edit_original(self, payload: dict):
        return await self._with_retry(
            "PATCH", f"/webhooks/{self.application_id}/{self.token}/messages/@original", payload)

    async def followup(self, payload: dict):
        return await self._with_retry(
            "POST", f"/webhooks/{self.application_id}/{self.token}", payload)


class _User:
    def __init__(self, uid: str):
        self.id = int(uid)

    def __int__(self):
        return self.id


class RelayComponentContext:
    """The slice of ``interactions.ComponentContext`` our component handlers
    use (``defer`` / ``send`` / ``edit_origin`` / ``user`` / ``values``)."""

    def __init__(self, payload: dict, defer_mode: str, responder: InteractionResponder):
        from utils.interaction_payload import interaction_user_id

        data = payload.get("data") or {}
        self.payload = payload
        self.custom_id = str(data.get("custom_id") or "")
        self.values = list(data.get("values") or [])
        self.user = self.author = _User(interaction_user_id(payload))
        self.guild_id = int(payload.get("guild_id") or 0)
        self.channel_id = int(payload.get("channel_id") or 0)
        self.defer_mode = defer_mode
        self.responder = responder
        # The sign-up handler reads this to choose edit-in-place vs a new reply.
        self.editing_origin = defer_mode == "update"
        self.deferred = True
        self._original_used = False

    async def defer(self, *_a, **_k) -> None:
        return None  # already deferred by the customer bot, as instructed

    @staticmethod
    def _payload(content=None, embeds=None, embed=None, components=None) -> dict:
        from interactions.models.discord.message import process_message_payload

        if embed is not None:
            embeds = [embed]
        body = process_message_payload(content=content, embeds=embeds, components=components)
        return core.rewrite_body(body, foreign_emojis())

    async def send(self, content=None, *, embeds=None, embed=None, components=None,
                   ephemeral: bool = False, **_ignored):
        body = self._payload(content, embeds, embed, components)
        if self.defer_mode == "ephemeral" and not self._original_used:
            # The private "thinking…" reply the customer bot opened IS this
            # answer — fill it in rather than stacking a second message.
            self._original_used = True
            return await self.responder.edit_original(body)
        if ephemeral:
            body["flags"] = int(body.get("flags") or 0) | 64
        return await self.responder.followup(body)

    async def edit_origin(self, content=None, *, embeds=None, embed=None, components=None,
                          **_ignored):
        body = self._payload(content, embeds, embed, components)
        if components is not None and "components" not in body:
            body["components"] = []
        if embeds is not None and "embeds" not in body:
            body["embeds"] = []
        self._original_used = True
        return await self.responder.edit_original(body)


def _event_in_group(session, event_id: int, group_id: int) -> bool:
    from db.models import Event

    ev = session.query(Event).filter(Event.id == int(event_id)).first()
    if ev is None:
        return False
    if ev.group_id == group_id:
        return True
    try:
        from db.models import EventGroup

        return session.query(EventGroup).filter(
            EventGroup.event_id == ev.id, EventGroup.group_id == group_id,
            EventGroup.status == "accepted").first() is not None
    except Exception:
        return False


def target_in_group(custom_id: str, group_id: int) -> bool:
    """The clicked thing must belong to the integration's group: a customer
    bot is trusted with WHO clicked, never with reaching other clans' data."""
    from db.models import Session

    s = Session()
    try:
        if custom_id.startswith("evtsignup"):
            rest = custom_id.split(":", 1)[1] if ":" in custom_id else ""
            event_id = rest.split(":", 1)[0]
            return event_id.isdigit() and _event_in_group(s, int(event_id), group_id)
        if custom_id.startswith("dtrv:"):
            from services import review_buttons_core as rbc

            parsed = rbc.parse_custom_id(custom_id)
            if parsed is None:
                return False
            if parsed.get("kind") == "drop":
                return int(parsed.get("group_id") or 0) == group_id
            from db.models import EventCompletion

            comp = s.query(EventCompletion).filter(
                EventCompletion.id == int(parsed.get("completion_id") or 0)).first()
            return comp is not None and _event_in_group(s, comp.event_id, group_id)
        return False
    except Exception:
        log.exception("relay: target check failed for %s", custom_id)
        return False
    finally:
        s.close()


async def dispatch_interaction(bot, entry: dict) -> str:
    """Run one forwarded click. Returns an outcome label (for logs/tests)."""
    payload = entry.get("payload") or {}
    facts = core.interaction_facts(payload)
    if time.time() - float(entry.get("received") or 0) > _INTERACTION_MAX_AGE_SECONDS:
        return "expired"
    responder = InteractionResponder(facts["application_id"], facts["token"])
    custom_id = facts["custom_id"]
    group_id = int(entry.get("group_id") or 0)
    if not await asyncio.to_thread(target_in_group, custom_id, group_id):
        try:
            await responder.followup({"content": "That button isn't available here.",
                                      "flags": 64})
        except Exception:
            pass
        return "refused"
    if custom_id.startswith("evtsignup"):
        ext = bot.get_ext("EventSignupButtons")
        if ext is None:
            return "no_handler"
        ctx = RelayComponentContext(payload, entry.get("defer") or "update", responder)
        await ext.handle_component(ctx)
        return "signup"
    if custom_id.startswith("dtrv:"):
        from services import review_buttons_core as rbc

        ext = bot.get_ext("ReviewButtons")
        parsed = rbc.parse_custom_id(custom_id)
        if ext is None or parsed is None:
            return "no_handler"
        await ext._handle(payload, parsed, responder=responder)
        return "review"
    return "unknown"


async def run_interaction_consumer(bot, *, block_seconds: int = 5) -> None:
    """Forever: pop forwarded clicks and answer them (started by bots/main)."""
    while True:
        r = _redis()
        if r is None:
            await asyncio.sleep(10)
            continue
        try:
            item = await asyncio.to_thread(r.brpop, core.INTERACTION_QUEUE, block_seconds)
        except Exception:
            log.exception("relay: interaction queue read failed")
            await asyncio.sleep(5)
            continue
        if not item:
            continue
        try:
            entry = json.loads(core._s(item[1]))
            outcome = await dispatch_interaction(bot, entry)
            log.info("relay: interaction %s -> %s",
                     (entry.get("payload") or {}).get("data", {}).get("custom_id"), outcome)
        except Exception:
            log.exception("relay: interaction dispatch failed")


# --------------------------------------------------------------------------- #
# Resync
# --------------------------------------------------------------------------- #

async def resync_group(bot, group_id: int, event_id: Optional[int] = None) -> int:
    """Forget relayed board messages that are gone, then redraw every live
    (or just-ended) event board the group hosts or takes part in."""
    from datetime import datetime, timedelta

    from db.models import Event, EventChannel, Session
    from services.event_board import RECENT_END_WINDOW_MINUTES, refresh_event_board

    s = Session()
    redrawn = 0
    try:
        cutoff = datetime.now() - timedelta(minutes=RECENT_END_WINDOW_MINUTES)
        q = (s.query(Event)
             .filter((Event.status == "active")
                     | ((Event.status == "past") & (Event.ended_at >= cutoff))))
        if event_id:
            q = q.filter(Event.id == int(event_id))
        r = _redis()
        for ev in q.all():
            if not _event_in_group(s, ev.id, group_id):
                continue
            for row in s.query(EventChannel).filter(EventChannel.event_id == ev.id,
                                                    EventChannel.kind == "leaderboard").all():
                if row.message_id and core.is_relay_message_id(row.message_id) and r is not None:
                    state = await asyncio.to_thread(core.target_state, r,
                                                    core.relay_mid(row.message_id))
                    if state != "live":
                        row.message_id = None
            s.commit()
            if await refresh_event_board(bot, s, ev, force=True):
                redrawn += 1
        return redrawn
    finally:
        s.close()


async def run_resync_consumer(bot, *, block_seconds: int = 5) -> None:
    while True:
        r = _redis()
        if r is None:
            await asyncio.sleep(10)
            continue
        try:
            item = await asyncio.to_thread(r.brpop, core.RESYNC_QUEUE, block_seconds)
        except Exception:
            log.exception("relay: resync queue read failed")
            await asyncio.sleep(5)
            continue
        if not item:
            continue
        try:
            entry = json.loads(core._s(item[1]))
            n = await resync_group(bot, int(entry["group_id"]), entry.get("event_id"))
            log.info("relay: resync for group %s redrew %s board(s)", entry["group_id"], n)
        except Exception:
            log.exception("relay: resync failed")
