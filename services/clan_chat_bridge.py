"""Two-way clan chat bridge: in-game clan chat ↔ a group's Discord channel.

Game → Discord: the plugin relays ``CLAN_CHAT`` player lines as
``type=clan_chat`` payloads; ``data/submissions/clan_chat.py`` authenticates
the relayer, binds groups, dedupes across relayers and calls
:func:`push_mirror_line`. ``CLAN_MESSAGE`` system broadcasts arrive on the
other intake (``type=clan_broadcast``, whose job is tracking non-plugin
clanmates) and are mirrored from there via :func:`mirror_broadcast_line` —
the channel is a SYNCED view of the clan chat box, so a broadcast belongs in
it whether or not the tracking half parses or records anything. Lines of both
kinds land on one Redis list (``chatbridge:out``) drained every couple of
seconds by the core bot (:func:`drain_and_send`), which batches per channel
into one message — a busy clan burst becomes one Discord send, not a
rate-limit pileup. Deliberately Redis-backed and lossy on restart: mirrored
chatter is ephemeral display, not data.

Discord → game: the core bot's MessageCreate listener matches messages
against :func:`bridge_channel_map` (60s-cached config scan), sanitizes, and
:func:`fan_out_discord_message` pushes a typed ``clan_chat_message`` envelope
into the plugin-notification inbox (``services/plugin_notifications``) of
every clan member whose plugin is PRESENT — presence being a sorted-set
heartbeat stamped by ``GET /notifications`` when the plugin polls with a
``clan`` parameter. Old plugin builds drop unknown envelope types silently,
so this ships without a client-version gate.

Loop safety is structural: the in-game rendering of a Discord line is
client-side only (never a real chat message), so relayers can't re-mirror it;
mirrored game lines are posted by the bot, and the listener ignores bots.

Module-level imports are stdlib-only (same contract as
plugin_notifications.py): anything Redis/DB/Discord-shaped is lazy-imported
inside functions so unit tests can load this file under the conftest stubs.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

#: Game → Discord staging list (JSON entries; see push_mirror_line).
MIRROR_LIST_KEY = "chatbridge:out"
#: Max entries pulled per drain pass; a deeper backlog just takes extra passes.
MIRROR_DRAIN_BATCH = 200
#: Cap on one batched Discord message (Discord's hard cap is 2000).
MIRROR_MESSAGE_MAX_CHARS = 1800

#: Staged line kinds: player speech (rendered with its sender) and clan system
#: broadcasts (no speaker — drops, pets, level-ups, joins, ...). Entries staged
#: before broadcasts were mirrored carry no kind and read as chat.
MIRROR_KIND_CHAT = "chat"
MIRROR_KIND_BROADCAST = "broadcast"
#: Marks a system broadcast in the mirror channel — the game colours these
#: differently in the chat box; Discord gets a prefix instead of a fake sender.
BROADCAST_PREFIX = "📢"

#: Per-relayer, per-minute ceiling on lines staged for the bridge. ONE budget
#: for both kinds: it is one relayer feeding one channel, and the thing being
#: capped is Discord spam. Chattier than the broadcast-tracking limit, still far
#: above any human clan's rate — a sustained breach is a spoof or a loop.
BRIDGE_RATE_LIMIT_PER_MIN = 120

#: Multi-relayer collapse window for broadcasts. Wider than the chat window
#: (``clan_chat.CHAT_SEEN_TTL_SECONDS``) because broadcasts are Jagex-generated:
#: an identical line inside a minute is the same event seen by a second relayer,
#: never two people typing the same thing.
BROADCAST_SEEN_TTL_SECONDS = 60

#: Presence heartbeat per clan: ZSET of player_ids scored by last-poll time.
PRESENCE_KEY_TEMPLATE = "chatbridge:presence:{clan_slug}"
PRESENCE_FRESH_SECONDS = 90
PRESENCE_KEY_TTL_SECONDS = 600

#: In-game chat renders ~80 chars per line; give Discord authors a little
#: more and let the client wrap, but stop walls of text cold.
DISCORD_TO_GAME_MAX_CHARS = 200

#: Group config keys (registry: web_api/config_registry.py).
BRIDGE_ENABLED_KEY = "clan_chat_bridge_enabled"
BRIDGE_CHANNEL_KEY = "channel_id_clan_chat_bridge"
CLAN_NAME_KEY = "clan_chat_name"

#: Envelope type for Discord→game lines (plugin renders as a clan-chat-styled
#: local message; unaware builds drop it).
ENVELOPE_TYPE = "clan_chat_message"

_channel_map_cache = {"expires": 0.0, "map": {}}
_CHANNEL_MAP_TTL_SECONDS = 60

_CUSTOM_EMOJI_RE = re.compile(r"<a?(:[A-Za-z0-9_~]+:)\d+>")
_USER_MENTION_RE = re.compile(r"<@!?\d+>")
_ROLE_MENTION_RE = re.compile(r"<@&\d+>")
_CHANNEL_MENTION_RE = re.compile(r"<#\d+>")
_MD_ESCAPE_RE = re.compile(r"([\\*_~`|>])")
_ANGLE_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def _redis():
    from utils.redis import RedisClient

    return RedisClient().client


# ── sanitizers (pure) ───────────────────────────────────────────────────────

def escape_markdown(text: str) -> str:
    """Escape Discord markdown in game-originated text (a player named
    ``*wave*`` must not italicize the mirror channel)."""
    return _MD_ESCAPE_RE.sub(r"\\\1", str(text or ""))


def sanitize_game_line(text: str) -> str:
    """Game chat line → safe Discord fragment. The relay forwards raw client
    text, so client markup tags are stripped before markdown escaping.
    Mentions can't fire regardless (sends use allowed_mentions none), but
    ``@everyone`` is neutralized visually too."""
    cleaned = _ANGLE_TAG_RE.sub("", str(text or ""))
    cleaned = cleaned.replace(" ", " ")
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    cleaned = escape_markdown(cleaned)
    return cleaned.replace("@everyone", "@​everyone").replace("@here", "@​here")


def sanitize_discord_content(content: str) -> str:
    """Discord message → one plain in-game-renderable line.

    Custom emoji collapse to ``:name:``, mentions to readable placeholders,
    newlines to `` | ``, and the result is length-capped — the client renders
    this as a single clan-chat line."""
    text = str(content or "")
    text = _CUSTOM_EMOJI_RE.sub(r"\1", text)
    text = _USER_MENTION_RE.sub("@user", text)
    text = _ROLE_MENTION_RE.sub("@role", text)
    text = _CHANNEL_MENTION_RE.sub("#channel", text)
    text = text.replace("\n", " | ")
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > DISCORD_TO_GAME_MAX_CHARS:
        text = text[: DISCORD_TO_GAME_MAX_CHARS - 1] + "…"
    return text


# ── presence (plugin ↔ clan) ────────────────────────────────────────────────

def stamp_presence(player_id, clan_slug: str) -> None:
    """Heartbeat: this player's plugin is online in this clan (called from
    ``GET /notifications`` when the poll carries a ``clan`` param).
    Best-effort — presence loss only delays Discord→game delivery."""
    if not player_id or not clan_slug:
        return
    try:
        key = PRESENCE_KEY_TEMPLATE.format(clan_slug=clan_slug)
        pipe = _redis().pipeline()
        pipe.zadd(key, {str(int(player_id)): time.time()})
        pipe.expire(key, PRESENCE_KEY_TTL_SECONDS)
        pipe.execute()
    except Exception:
        pass


def online_player_ids(clan_slug: str) -> list:
    """Player ids whose plugin heartbeat for this clan is fresh."""
    if not clan_slug:
        return []
    try:
        key = PRESENCE_KEY_TEMPLATE.format(clan_slug=clan_slug)
        now = time.time()
        client = _redis()
        client.zremrangebyscore(key, "-inf", now - PRESENCE_KEY_TTL_SECONDS)
        members = client.zrangebyscore(key, now - PRESENCE_FRESH_SECONDS, "+inf")
        out = []
        for member in members or []:
            try:
                if isinstance(member, bytes):
                    member = member.decode("utf-8")
                out.append(int(member))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        return []


# ── group binding ───────────────────────────────────────────────────────────

def bridge_bound_groups(session, relayer_player_id, clan_slug: str) -> dict:
    """``{group_id: channel_id}`` of the RELAYER's groups that bridged this
    clan: bridge enabled + channel set + ``clan_chat_name`` matches. Same
    trust shape as broadcast binding — a line only reaches channels of groups
    the authed relayer belongs to."""
    from sqlalchemy import text as sql_text

    from utils import group_config as gc
    from utils.clan_broadcasts import clan_slug as make_slug

    rows = session.execute(
        sql_text("SELECT DISTINCT group_id FROM user_group_association WHERE player_id = :pid"),
        {"pid": int(relayer_player_id)},
    ).all()
    candidate_ids = [gid for (gid,) in rows if gid and gid > 2]
    if not candidate_ids:
        return {}
    values = gc.get_bulk(
        session, candidate_ids, [BRIDGE_ENABLED_KEY, BRIDGE_CHANNEL_KEY, CLAN_NAME_KEY]
    )
    bound = {}
    for gid in candidate_ids:
        if not gc.is_truthy(values.get((gid, BRIDGE_ENABLED_KEY))):
            continue
        channel_id = str(values.get((gid, BRIDGE_CHANNEL_KEY)) or "").strip()
        if channel_id in ("", "0"):
            continue
        if make_slug(values.get((gid, CLAN_NAME_KEY)) or "") != clan_slug:
            continue
        bound[gid] = channel_id
    return bound


def bridge_channel_map(session) -> dict:
    """``{channel_id_str: (group_id, clan_slug)}`` for every fully-configured
    bridge — the MessageCreate listener's routing table. Cached in-process for
    60s so the listener never queries per message."""
    now = time.monotonic()
    if now < _channel_map_cache["expires"]:
        return _channel_map_cache["map"]

    from db.models import GroupConfiguration
    from utils.clan_broadcasts import clan_slug as make_slug

    result = {}
    try:
        rows = (
            session.query(
                GroupConfiguration.group_id,
                GroupConfiguration.config_key,
                GroupConfiguration.config_value,
            )
            .filter(
                GroupConfiguration.config_key.in_(
                    [BRIDGE_ENABLED_KEY, BRIDGE_CHANNEL_KEY, CLAN_NAME_KEY]
                )
            )
            .all()
        )
        by_group: dict = {}
        for gid, key, value in rows:
            by_group.setdefault(gid, {})[key] = value
        for gid, values in by_group.items():
            if str(values.get(BRIDGE_ENABLED_KEY) or "").strip().lower() not in ("1", "true"):
                continue
            channel_id = str(values.get(BRIDGE_CHANNEL_KEY) or "").strip()
            slug = make_slug(values.get(CLAN_NAME_KEY) or "")
            if channel_id in ("", "0") or not slug:
                continue
            result[channel_id] = (gid, slug)
        _channel_map_cache["map"] = result
        _channel_map_cache["expires"] = now + _CHANNEL_MAP_TTL_SECONDS
    except Exception as e:
        print(f"[ClanChatBridge] channel map refresh failed: {e}")
        # A failed transaction left on the shared scoped session would make
        # every future refresh fail too — clear it so the next 60s expiry
        # can actually recover instead of serving the stale map forever.
        try:
            session.rollback()
        except Exception:
            pass
        return _channel_map_cache["map"]
    return result


def invalidate_channel_map() -> None:
    _channel_map_cache["expires"] = 0.0


# ── game → Discord ──────────────────────────────────────────────────────────

def relayer_within_rate_limit(relayer_player_id) -> bool:
    """Per-relayer ceiling on lines staged for the bridge this minute.

    Shared by both game→Discord intakes (chat lines and broadcast mirroring),
    so one relayer cannot dodge the cap by spreading traffic across the two.
    Fails open — Redis trouble must not silence a clan's chat."""
    try:
        minute = int(time.time() // 60)
        key = f"chatbridge:rate:{int(relayer_player_id)}:{minute}"
        client = _redis()
        count = client.incr(key)
        if count == 1:
            client.expire(key, 120)
        return int(count) <= BRIDGE_RATE_LIMIT_PER_MIN
    except Exception:
        return True


def _claim_first_sight(key: str, ttl: int) -> bool:
    """``SET NX`` claim so only the first relayer's copy of a line is staged.
    Fails open: a doubled display line beats a missing one."""
    try:
        return bool(_redis().set(key, "1", nx=True, ex=ttl))
    except Exception:
        return True


def _stage_entry(group_id, channel_id, kind, message, sender=None, rank=None) -> bool:
    try:
        entry = {
            "group_id": int(group_id),
            "channel_id": str(channel_id),
            "kind": kind,
            "message": str(message or ""),
            "ts": int(time.time()),
        }
        if kind == MIRROR_KIND_CHAT:
            entry["sender"] = str(sender or "")[:32]
            entry["rank"] = str(rank or "")[:32] or None
        client = _redis()
        pipe = client.pipeline()
        pipe.rpush(MIRROR_LIST_KEY, json.dumps(entry))
        # Backstop cap: if the bot is down, don't grow unbounded — old chatter
        # is worthless once it's minutes stale.
        pipe.ltrim(MIRROR_LIST_KEY, -2000, -1)
        pipe.execute()
        return True
    except Exception as e:
        print(f"[ClanChatBridge] mirror push failed: {e}")
        return False


def push_mirror_line(group_id, channel_id, sender, message, rank=None) -> bool:
    """Stage one game chat line (player speech) for the batched channel send."""
    return _stage_entry(
        group_id, channel_id, MIRROR_KIND_CHAT, message, sender=sender, rank=rank
    )


def push_mirror_broadcast(group_id, channel_id, message) -> bool:
    """Stage one clan system broadcast (no speaker) for the batched send."""
    return _stage_entry(group_id, channel_id, MIRROR_KIND_BROADCAST, message)


def mirror_broadcast_line(session, relayer_player_id, clan_slug: str, message: str) -> int:
    """Mirror one ``CLAN_MESSAGE`` broadcast into this clan's bridge channels.

    Called from the clan_broadcast intake ahead of every tracking decision —
    parse, tracked-kind filter, group binding, record, notify — because the
    bridge is a synced chat view, not a record of what we tracked. Returns the
    number of channels staged (0 = no bridged group, or another relayer's copy
    got there first).

    The first-sight claim is per GROUP, not per clan: N clanmates relay one
    broadcast and each channel must show it once, but two groups bridging the
    same clan through different relayers must both receive it.
    """
    if not clan_slug or not str(message or "").strip():
        return 0
    bound = bridge_bound_groups(session, relayer_player_id, clan_slug)
    if not bound:
        return 0
    digest = hashlib.sha256(str(message).encode("utf-8")).hexdigest()[:24]
    staged = 0
    for group_id, channel_id in bound.items():
        if not _claim_first_sight(
            f"chatbridge:seenbc:{group_id}:{digest}", BROADCAST_SEEN_TTL_SECONDS
        ):
            continue
        if push_mirror_broadcast(group_id, channel_id, message):
            staged += 1
    return staged


def drain_mirror_lines(limit: int = MIRROR_DRAIN_BATCH) -> list:
    """Pop up to ``limit`` staged lines (FIFO, single-consumer)."""
    try:
        pipe = _redis().pipeline()
        pipe.lrange(MIRROR_LIST_KEY, 0, int(limit) - 1)
        pipe.ltrim(MIRROR_LIST_KEY, int(limit), -1)
        raw_items = pipe.execute()[0] or []
    except Exception:
        return []
    entries = []
    for item in raw_items:
        try:
            if isinstance(item, bytes):
                item = item.decode("utf-8")
            entries.append(json.loads(item))
        except Exception:
            continue
    return entries


def batch_lines_by_channel(entries: list, rank_emojis: dict = None) -> dict:
    """``{channel_id: [rendered_line, ...]}`` — pure formatting step.

    Lines arrive pre-sanitized relative to the GAME (client markup already
    meaningless) but not Discord: sender and message are markdown-escaped
    here, at the last moment before send. Broadcasts have no sender and render
    with :data:`BROADCAST_PREFIX` instead, keeping system lines visually apart
    from player speech the way the game's chat colours do.

    A staged rank renders as a leading app emoji (``:rank: **Name**: msg``) —
    the emoji token is built after escaping, never through it, or the escaper
    would break the ``<:name:id>`` syntax. Pass ``rank_emojis`` to keep this
    pure; the default loads the seeded map."""
    from utils.rank_emojis import emoji_for_rank

    batches: dict = {}
    for entry in entries:
        channel_id = str(entry.get("channel_id") or "")
        message = sanitize_game_line(entry.get("message"))
        if not channel_id or not message:
            continue
        if str(entry.get("kind") or MIRROR_KIND_CHAT) == MIRROR_KIND_BROADCAST:
            batches.setdefault(channel_id, []).append(f"{BROADCAST_PREFIX} *{message}*")
            continue
        sender = sanitize_game_line(entry.get("sender"))
        if not sender:
            continue
        icon = emoji_for_rank(entry.get("rank"), rank_emojis)
        line = f"**{sender}**: {message}"
        batches.setdefault(channel_id, []).append(f"{icon} {line}" if icon else line)
    return batches


async def drain_and_send(bot) -> int:
    """One drain pass: batch staged lines per channel, one send per channel.
    Called from a core-bot interval task. Returns messages sent."""
    entries = drain_mirror_lines()
    if not entries:
        return 0
    sent = 0
    for channel_id, lines in batch_lines_by_channel(entries).items():
        content = ""
        for line in lines:
            if len(content) + len(line) + 1 > MIRROR_MESSAGE_MAX_CHARS:
                sent += await _send_mirror_message(bot, channel_id, content)
                content = ""
            content = f"{content}\n{line}" if content else line
        if content:
            sent += await _send_mirror_message(bot, channel_id, content)
    return sent


async def _send_mirror_message(bot, channel_id, content) -> int:
    try:
        from interactions import AllowedMentions

        channel = await bot.fetch_channel(int(channel_id))
        if channel is None:
            return 0
        await channel.send(content, allowed_mentions=AllowedMentions.none())
        return 1
    except Exception as e:
        print(f"[ClanChatBridge] mirror send to {channel_id} failed: {e}")
        return 0


# ── Discord → game ──────────────────────────────────────────────────────────

def fan_out_discord_message(clan_slug: str, sender: str, content: str) -> int:
    """Push one Discord line to every present clan member's plugin inbox.
    Returns inboxes pushed; 0 when nobody's plugin is online (the message
    simply doesn't reach the game — there is no backfill, like real chat)."""
    message = sanitize_discord_content(content)
    if not message:
        return 0
    from services.plugin_notifications import build_envelope, push_to_inbox

    envelope = build_envelope(
        ENVELOPE_TYPE,
        {"sender": str(sender or "Discord")[:32], "message": message},
    )
    delivered = 0
    for player_id in online_player_ids(clan_slug):
        if push_to_inbox(player_id, envelope):
            delivered += 1
    return delivered
