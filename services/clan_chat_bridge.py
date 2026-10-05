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

Loop safety takes two guards, because only ONE of the directions closes
itself: mirrored game lines are posted by the bot and the MessageCreate
listener ignores bots, so Discord→game can't be re-fed. The other way round
is not free — the plugin renders a Discord line client-side, but it renders
it through ``client.addChatMessage``, which posts a real ``ChatMessage``
event, so the line lands back in the relayer's own CLAN_CHAT subscriber and
without a guard is relayed straight back to the channel it was typed in. The
rendered sender carries :data:`ECHO_SENDER_MARKER`; the plugin drops those
before relaying and this intake drops them again (:func:`is_bridge_echo`),
which is what covers the installs still running a pre-fix build.

Bots and webhooks are ignored on the Discord side unless the group
allowlisted that exact ID (``clan_chat_bridge_allowed_bots``, empty by
default; /clan-bridge or the website). Our own application can never be
admitted (:func:`allowed_bot_source`), so the first guard holds. An allowed
bot that is itself a game→Discord bridge would re-post our clan's chat, so
every staged line is also remembered per channel for a few minutes and a bot
message repeating one is dropped (:func:`is_recent_mirror_echo`), with a
per-bot rate limit as the backstop.

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
#: Leads a system broadcast in the mirror channel when nothing better fits —
#: the game colours these differently in the chat box; Discord gets an icon
#: instead of a fake sender. Recognized kinds get their own icon instead (see
#: :func:`broadcast_icon`).
BROADCAST_PREFIX = "📢"

#: Per-relayer, per-minute ceiling on lines staged for the bridge. ONE budget
#: for both kinds: it is one relayer feeding one channel, and the thing being
#: capped is Discord spam. Chattier than the broadcast-tracking limit, still far
#: above any human clan's rate — a sustained breach is a spoof or a loop.
BRIDGE_RATE_LIMIT_PER_MIN = 120

#: Multi-relayer collapse window for broadcasts. Same 60s as the chat window
#: (``clan_chat.CHAT_SEEN_TTL_SECONDS``, which was widened to match after the
#: shorter one leaked duplicates): an identical line inside a minute from a
#: SECOND relayer is the same event seen twice. A repeat from the same relayer
#: is a second event and still mirrors (:func:`claim_relayed_line`).
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
#: Comma-separated Discord IDs (bot users or webhooks) whose messages in the
#: bridge channel are relayed into the game like a member's. Empty, the
#: default, means bots are ignored as they always were. Bridge channel only.
BRIDGE_ALLOWED_BOTS_KEY = "clan_chat_bridge_allowed_bots"
#: 10 snowflakes plus commas stays inside config_value's VARCHAR(255).
MAX_ALLOWED_BOTS = 10

#: Per-bot, per-channel ceiling on lines relayed into the game. Far above any
#: announcement bot; mainly a circuit breaker for two bridges feeding each
#: other (see :func:`is_recent_mirror_echo`).
BOT_RELAY_RATE_LIMIT_PER_MIN = 30

#: What the bot recently mirrored game→Discord into each bridge channel, so an
#: allowlisted bot that re-posts the same clan chat (another game bridge) is
#: not relayed back into the game as a duplicate line.
RECENT_MIRROR_KEY_TEMPLATE = "chatbridge:recent:{channel_id}"
RECENT_MIRROR_KEEP = 60
RECENT_MIRROR_TTL_SECONDS = 180
#: A remembered line shorter than this only matches a bot message EXACTLY —
#: "gz" must not swallow every bot post that contains "gz".
RECENT_MIRROR_MIN_SUBSTRING = 12

#: Envelope type for Discord→game lines (plugin renders as a clan-chat-styled
#: local message; unaware builds drop it).
ENVELOPE_TYPE = "clan_chat_message"

#: Stamped onto the Discord author's name by the plugin's renderer
#: (``ChatMessageUtil.DISCORD_SENDER_MARKER``), and the only thing separating
#: our own echoed line from a clanmate's. An OSRS display name is letters,
#: digits, spaces, hyphens and underscores — never parentheses — so this
#: marker cannot collide with a real sender.
ECHO_SENDER_MARKER = "(Discord)"

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


_SNOWFLAKE_RE = re.compile(r"^\d{15,21}$")


def parse_allowed_bots(raw) -> frozenset:
    """The stored allowlist → a set of snowflake strings. Separators are
    forgiving (commas, spaces, newlines); anything that isn't a snowflake is
    dropped rather than failing the whole list."""
    ids = []
    for part in re.split(r"[\s,]+", str(raw or "")):
        part = part.strip().strip("<@!&>")
        if _SNOWFLAKE_RE.match(part) and part not in ids:
            ids.append(part)
    return frozenset(ids[:MAX_ALLOWED_BOTS])


def add_allowed_bot(raw, bot_id) -> tuple:
    """(new stored value, status) after adding ``bot_id`` to the stored
    allowlist. status: ``added`` | ``already`` | ``full`` | ``invalid``."""
    bot_id = str(bot_id or "").strip()
    current = allowed_bot_list(raw)
    if not _SNOWFLAKE_RE.match(bot_id):
        return ",".join(current), "invalid"
    if bot_id in current:
        return ",".join(current), "already"
    if len(current) >= MAX_ALLOWED_BOTS:
        return ",".join(current), "full"
    return ",".join(current + [bot_id]), "added"


def remove_allowed_bot(raw, bot_id) -> tuple:
    """(new stored value, removed?) after taking ``bot_id`` off the list."""
    bot_id = str(bot_id or "").strip()
    current = allowed_bot_list(raw)
    if bot_id not in current:
        return ",".join(current), False
    return ",".join(i for i in current if i != bot_id), True


def allowed_bot_list(raw) -> list:
    """The stored list in its saved order (parse_allowed_bots is a set)."""
    allowed = parse_allowed_bots(raw)
    seen: list = []
    for part in re.split(r"[\s,]+", str(raw or "")):
        part = part.strip().strip("<@!&>")
        if part in allowed and part not in seen:
            seen.append(part)
    return seen


def bridge_channel_map(session=None) -> dict:
    """``{channel_id_str: (group_id, clan_slug, allowed_bot_ids)}`` for every
    fully-configured bridge — the MessageCreate listener's routing table.
    ``allowed_bot_ids`` is a frozenset of bot-user / webhook IDs (usually
    empty). Cached in-process for 60s so the listener never queries per
    message.

    Owns a fresh session unless the caller supplies one. The listener calls this
    under ``asyncio.to_thread``, and a *scoped* session touched on a pool worker
    thread is cleaned up by nothing — the registry holds it for the life of the
    thread, so the read below autobegins a transaction that never ends. That is
    the 2026-08-25 incident: ~15 permanently idle-in-transaction connections
    blocked InnoDB purge for 7h, drove the history list to 797k, and starved
    both submission processors until the bot was restarted.
    """
    now = time.monotonic()
    if now < _channel_map_cache["expires"]:
        return _channel_map_cache["map"]

    from db.models import GroupConfiguration, Session
    from utils.clan_broadcasts import clan_slug as make_slug

    owns_session = session is None
    if owns_session:
        session = Session()

    result = {}
    try:
        rows = (
            session.query(
                GroupConfiguration.group_id,
                GroupConfiguration.config_key,
                GroupConfiguration.config_value,
                GroupConfiguration.long_value,
            )
            .filter(
                GroupConfiguration.config_key.in_(
                    [BRIDGE_ENABLED_KEY, BRIDGE_CHANNEL_KEY, CLAN_NAME_KEY,
                     BRIDGE_ALLOWED_BOTS_KEY]
                )
            )
            .all()
        )
        by_group: dict = {}
        for gid, key, value, long_value in rows:
            by_group.setdefault(gid, {})[key] = value or long_value
        for gid, values in by_group.items():
            if str(values.get(BRIDGE_ENABLED_KEY) or "").strip().lower() not in ("1", "true"):
                continue
            channel_id = str(values.get(BRIDGE_CHANNEL_KEY) or "").strip()
            slug = make_slug(values.get(CLAN_NAME_KEY) or "")
            if channel_id in ("", "0") or not slug:
                continue
            allowed = parse_allowed_bots(values.get(BRIDGE_ALLOWED_BOTS_KEY))
            result[channel_id] = (gid, slug, allowed)
        _channel_map_cache["map"] = result
        _channel_map_cache["expires"] = now + _CHANNEL_MAP_TTL_SECONDS
    except Exception as e:
        print(f"[ClanChatBridge] channel map refresh failed: {e}")
        # A failed transaction left on a caller-owned session would make every
        # future refresh fail too — clear it so the next 60s expiry can
        # actually recover instead of serving the stale map forever.
        try:
            session.rollback()
        except Exception:
            pass
        return _channel_map_cache["map"]
    finally:
        # Rolls back the read that `.query()` autobegan and hands the
        # connection back to the pool. Without this the *success* path leaks:
        # the old code only rolled back when the query raised.
        if owns_session:
            session.close()
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


#: Atomic per-relayer occurrence count for one line. Each relayer's client
#: sees every copy of a line exactly once, so the number of copies that really
#: happened is the HIGHEST count any single relayer has reported — a line is
#: shown when its relayer's count passes the number already shown. The window
#: is fixed from the first sighting (EXPIRE only on create), matching the old
#: SET NX EX claim it replaces.
_CLAIM_LINE_LUA = """
local n = redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
local shown = tonumber(redis.call('HGET', KEYS[1], '_shown') or '0')
if n > shown then
  redis.call('HSET', KEYS[1], '_shown', n)
  return 1
end
return 0
"""


def claim_relayed_line(key: str, relayer_id, ttl: int) -> bool:
    """Whether this relayer's copy of a line should be staged.

    The dedupe exists to collapse the copies N clanmates' plugins relay of ONE
    line — never to second-guess a single client. So the same relayer sending
    the same text twice ("gz", then "gz" again) means it really appeared
    twice and both copies mirror, while a second relayer's copies only mirror
    once they outnumber what has already been shown. Fails open: a doubled
    display line beats a missing one."""
    try:
        return bool(_redis().eval(_CLAIM_LINE_LUA, 1, key, str(relayer_id), int(ttl)))
    except Exception:
        return True


def _stage_entry(group_id, channel_id, kind, message, sender=None, rank=None,
                 extra: dict = None) -> bool:
    from utils.mirror_context import is_mirrored_submission

    # Mirrored production traffic never reaches the bridge. Staged entries are
    # drained by drain_and_send(), which resolves the channel through the bot —
    # and a cache hit on bot.get_channel() bypasses the dev guild guard entirely,
    # so a mirrored clan-chat line could be relayed into a real clan's Discord.
    if is_mirrored_submission():
        return False
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
        # Render hints (broadcast kind, item/NPC for the icon, account type for
        # the name badge). Optional: entries staged without them render plain.
        for field, value in (extra or {}).items():
            if value:
                entry[field] = str(value)[:64]
        client = _redis()
        pipe = client.pipeline()
        pipe.rpush(MIRROR_LIST_KEY, json.dumps(entry))
        # Backstop cap: if the bot is down, don't grow unbounded — old chatter
        # is worthless once it's minutes stale.
        pipe.ltrim(MIRROR_LIST_KEY, -2000, -1)
        _remember_mirrored(pipe, entry)
        pipe.execute()
        return True
    except Exception as e:
        print(f"[ClanChatBridge] mirror push failed: {e}")
        return False


_NORMALIZE_STRIP_RE = re.compile(r"[^a-z0-9]+")


def normalize_for_echo(text) -> str:
    """Case, markup and punctuation folded away: lowercase alphanumeric words
    separated by single spaces. Both sides of the echo check go through this,
    so another bridge's "**Bob**: hi!" and our "Bob hi" compare equal."""
    text = _ANGLE_TAG_RE.sub(" ", _CUSTOM_EMOJI_RE.sub(r"\1", str(text or "")))
    return _NORMALIZE_STRIP_RE.sub(" ", text.lower()).strip()


def _echo_forms(entry: dict) -> list:
    """The normalized texts an echo of this staged entry could look like."""
    message = normalize_for_echo(entry.get("message"))
    if not message:
        return []
    forms = [message]
    sender = normalize_for_echo(entry.get("sender"))
    if sender:
        forms.append(f"{sender} {message}")
    return forms


def _remember_mirrored(pipe, entry: dict) -> None:
    """Queue (on the staging pipeline) this entry's echo forms onto its
    channel's short recent-mirror list. Read only for allowlisted bots."""
    forms = _echo_forms(entry)
    if not forms:
        return
    key = RECENT_MIRROR_KEY_TEMPLATE.format(channel_id=entry["channel_id"])
    pipe.lpush(key, *forms)
    pipe.ltrim(key, 0, RECENT_MIRROR_KEEP - 1)
    pipe.expire(key, RECENT_MIRROR_TTL_SECONDS)


def matches_recent_mirror(text: str, recent) -> bool:
    """Whether a bot message (already normalized) repeats a line we mirrored.

    Exact match always counts. A prefixed copy ("clan bob hi" for "bob hi")
    counts when the remembered form is a whole-word suffix, and a long enough
    remembered line counts anywhere inside the message. Short lines need an
    exact match so "gz" never eats an unrelated bot post."""
    if not text:
        return False
    for remembered in recent or ():
        if isinstance(remembered, bytes):
            remembered = remembered.decode("utf-8", "replace")
        if not remembered:
            continue
        if text == remembered or text.endswith(" " + remembered):
            return True
        if len(remembered) >= RECENT_MIRROR_MIN_SUBSTRING and remembered in text:
            return True
    return False


def is_recent_mirror_echo(channel_id, text: str) -> bool:
    """Whether an allowlisted bot's message in ``channel_id`` is a copy of game
    chat we just mirrored there — i.e. the bot is another game→Discord bridge,
    and relaying it would show the clan every line twice. Fails open (not an
    echo) when Redis is unavailable; the per-bot rate limit still holds."""
    normalized = normalize_for_echo(text)
    if not normalized:
        return False
    try:
        key = RECENT_MIRROR_KEY_TEMPLATE.format(channel_id=channel_id)
        recent = _redis().lrange(key, 0, RECENT_MIRROR_KEEP - 1)
    except Exception:
        return False
    return matches_recent_mirror(normalized, recent)


def bot_within_rate_limit(channel_id, author_id) -> bool:
    """Per-(channel, bot) ceiling on lines relayed into the game per minute.
    Fails open, like the relayer limit."""
    try:
        minute = int(time.time() // 60)
        key = f"chatbridge:botrate:{channel_id}:{author_id}:{minute}"
        client = _redis()
        count = client.incr(key)
        if count == 1:
            client.expire(key, 120)
        return int(count) <= BOT_RELAY_RATE_LIMIT_PER_MIN
    except Exception:
        return True


def push_mirror_line(group_id, channel_id, sender, message, rank=None,
                     account_type=None) -> bool:
    """Stage one game chat line (player speech) for the batched channel send."""
    return _stage_entry(
        group_id, channel_id, MIRROR_KIND_CHAT, message, sender=sender, rank=rank,
        extra={"account_type": account_type},
    )


def push_mirror_broadcast(group_id, channel_id, message, extra: dict = None) -> bool:
    """Stage one clan system broadcast (no speaker) for the batched send.
    ``extra`` carries the render hints from :func:`broadcast_hints`."""
    return _stage_entry(group_id, channel_id, MIRROR_KIND_BROADCAST, message, extra=extra)


# ── broadcast icons ─────────────────────────────────────────────────────────

#: Shapes the tracking parser (``utils.clan_broadcasts``) does not classify,
#: recognized here for the icon only. Kept out of the parser on purpose: a
#: parser match changes what TRACKING does with a line, and a display tweak
#: must never start recording e.g. ToA personal bests.
_DISPLAY_KIND_PATTERNS = (
    (re.compile(r" achieved a new .*personal best", re.IGNORECASE), "personal_best"),
    (re.compile(r" tier of rewards from Combat Achievements", re.IGNORECASE),
     "combat_achievement"),
    (re.compile(r" has opened a loot key", re.IGNORECASE), "pk"),
    (re.compile(r" has (?:been )?defeated ", re.IGNORECASE), "pk"),
    (re.compile(r" has completed a quest", re.IGNORECASE), "quest"),
)

#: Broadcast kind → app emoji key (utils/app_emojis.SPECS).
_KIND_APP_EMOJI = {
    "collection_log": "collection_log",
    "combat_achievement": "combat_achievement",
    "quest": "quest",
    "diary": "diary",
    "level_up": "stats",
    "xp_milestone": "stats",
    "pk": "skull",
}

#: Broadcast kind → plain Unicode icon, for kinds with no game art of their own.
_KIND_UNICODE = {
    "item_drop": "💰",
    "raid_drop": "💰",
    "clue_item": "💰",
    "pet": "🐾",
    "personal_best": "⏱️",
    "coffer_donation": "💰",
    "coffer_withdrawal": "💰",
    "invite": "🤝",
    "left_clan": "🚪",
    "expelled": "🚫",
}


def broadcast_hints(parsed, message: str) -> dict:
    """Render hints staged with a broadcast: which icon family it gets and the
    item/NPC name that picks a game glyph. ``parsed`` is the tracking parser's
    result (or None); unparsed lines fall back to the display-only shapes."""
    kind = getattr(parsed, "kind", None)
    if not kind:
        for pattern, display_kind in _DISPLAY_KIND_PATTERNS:
            if pattern.search(str(message or "")):
                kind = display_kind
                break
    hints = {"bkind": kind, "subject": getattr(parsed, "player", None)}
    if kind in ("item_drop", "raid_drop", "clue_item", "pet"):
        hints["item"] = getattr(parsed, "item_name", None)
    elif kind == "personal_best" and parsed is not None:
        hints["npc"] = (getattr(parsed, "extra", None) or {}).get("activity")
    elif kind in ("coffer_donation", "coffer_withdrawal"):
        hints["item"] = "Coins"
    return hints


def broadcast_icon(entry: dict) -> str:
    """The leading icon for a staged broadcast.

    Drops and pets use the item's own glyph, personal bests the boss's, when
    the game emoji set has one (it is a budget, not a catalogue); otherwise
    each kind has its own icon, and only lines nobody recognizes keep the
    generic :data:`BROADCAST_PREFIX`."""
    from utils import app_emojis, game_emojis

    kind = entry.get("bkind")
    glyph = None
    if entry.get("item"):
        glyph = game_emojis.emoji_for_item(entry["item"])
    elif entry.get("npc"):
        glyph = game_emojis.emoji_for_npc(entry["npc"])
    if glyph:
        return glyph
    if kind in _KIND_APP_EMOJI:
        return app_emojis.emoji(_KIND_APP_EMOJI[kind])
    return _KIND_UNICODE.get(kind) or BROADCAST_PREFIX


def account_badge(account_type) -> str:
    """The game-mode badge drawn before a name, or "" for none/unseeded."""
    if not account_type or account_type == "normal":
        return ""
    from utils import app_emojis

    return app_emojis.seeded_emoji(str(account_type)) or ""


def mirror_broadcast_line(session, relayer_player_id, clan_slug: str, message: str,
                          parsed=None) -> int:
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
    hints = broadcast_hints(parsed, message)
    staged = 0
    for group_id, channel_id in bound.items():
        if not claim_relayed_line(
            f"chatbridge:seenbc:{group_id}:{digest}",
            relayer_player_id,
            BROADCAST_SEEN_TTL_SECONDS,
        ):
            continue
        extra = dict(hints)
        if hints.get("subject"):
            extra["account_type"] = _subject_account_type(
                session, group_id, hints["subject"]
            )
        if push_mirror_broadcast(group_id, channel_id, message, extra=extra):
            staged += 1
    return staged


def _subject_account_type(session, group_id, player_name):
    """Badge lookup for a broadcast's subject; cosmetic, so never raises."""
    try:
        from utils.clan_ranks import account_type_for_group_member

        return account_type_for_group_member(session, group_id, player_name)
    except Exception:
        return None


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
    here, at the last moment before send. Broadcasts have no sender and lead
    with a kind icon instead (:func:`broadcast_icon`), in plain text: the
    missing bold ``Name:`` already says nobody typed it, the way the game's
    chat colours do.

    A staged rank renders as a leading app emoji (``:rank: **Name**: msg``),
    then the account-type badge, as the game draws them — the emoji tokens
    are built after escaping, never through it, or the escaper would break
    the ``<:name:id>`` syntax. Pass ``rank_emojis`` to keep this pure; the
    default loads the seeded map."""
    from utils.rank_emojis import emoji_for_rank

    batches: dict = {}
    for entry in entries:
        channel_id = str(entry.get("channel_id") or "")
        message = sanitize_game_line(entry.get("message"))
        if not channel_id or not message:
            continue
        badge = account_badge(entry.get("account_type"))
        if str(entry.get("kind") or MIRROR_KIND_CHAT) == MIRROR_KIND_BROADCAST:
            subject = sanitize_game_line(entry.get("subject"))
            if badge and subject and message.startswith(subject):
                message = f"{badge} {message}"
            batches.setdefault(channel_id, []).append(f"{broadcast_icon(entry)} {message}")
            continue
        sender = sanitize_game_line(entry.get("sender"))
        if not sender:
            continue
        icons = [i for i in (emoji_for_rank(entry.get("rank"), rank_emojis), badge) if i]
        line = f"**{sender}**: {message}"
        batches.setdefault(channel_id, []).append(" ".join(icons + [line]))
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

def is_bridge_echo(sender) -> bool:
    """Whether a relayed game line is our own Discord→game render coming back.

    See the module docstring: the plugin's local render fires a real chat
    event, so an install without the client-side guard relays the line to us.
    Substring rather than suffix — the marker trails the name today, but the
    sender is length-capped on both hops and the test has to survive a cut."""
    return ECHO_SENDER_MARKER in str(sender or "")


#: Message types an allowlisted bot can relay: plain posts, replies, and the
#: visible responses to slash / context-menu commands. Everything else (pins,
#: joins, boosts, thread notices) is a system message, not speech.
_RELAYABLE_BOT_MESSAGE_TYPES = frozenset({0, 19, 20, 23})


def allowed_bot_source(message, allowed_ids, own_ids=()) -> "str | None":
    """Which allowlist entry admits this bot/webhook message, or None.

    A webhook post is matched by its webhook ID (its author ID is the same
    number), a bot's post by the bot's user ID. Our own application can never
    be admitted, whatever the list says: the bot's mirrored game lines are
    posted in this very channel, and relaying them would loop."""
    if not allowed_ids:
        return None
    author = getattr(message, "author", None)
    author_id = str(getattr(author, "id", "") or "")
    application_id = str(getattr(message, "application_id", "") or "")
    own = {str(i) for i in own_ids if i}
    if author_id in own or (application_id and application_id in own):
        return None
    try:
        message_type = int(getattr(message, "type", 0) or 0)
    except (TypeError, ValueError):
        message_type = 0
    if message_type not in _RELAYABLE_BOT_MESSAGE_TYPES:
        return None
    webhook_id = str(getattr(message, "webhook_id", "") or "")
    for candidate in (webhook_id, author_id):
        if candidate and candidate in allowed_ids:
            return candidate
    return None


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:https?://|<)[^)]*\)")
_MD_HEADING_RE = re.compile(r"^\s*(?:-#|#{1,3})\s+", re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"(\*\*|__|~~|\|\||`+|\*)")


def strip_markdown(text: str) -> str:
    """Bot posts lean on markdown that would show as literal ``**`` in game.
    Links keep their label, headings and emphasis markers go."""
    text = _MD_LINK_RE.sub(r"\1", str(text or ""))
    text = _MD_HEADING_RE.sub("", text)
    return _MD_EMPHASIS_RE.sub("", text)


def _field(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _component_texts(components, out: list, depth: int = 0) -> None:
    """Text Display contents from a Components V2 tree, in order."""
    if depth > 4:
        return
    for component in components or ():
        content = _field(component, "content")
        if isinstance(content, str) and content.strip():
            out.append(content.strip())
        _component_texts(_field(component, "components"), out, depth + 1)


def bot_message_text(message) -> str:
    """The speakable text of a bot/webhook message.

    Plain content wins. Bots often post an embed (or a Components V2 layout)
    with no content at all, so fall back to the first embed's author, title
    and description, then to V2 text blocks. Markdown is stripped here; the
    usual sanitizer still runs afterwards."""
    text = str(getattr(message, "content", "") or "").strip()
    if not text:
        embeds = getattr(message, "embeds", None) or []
        if embeds:
            embed = embeds[0]
            parts = []
            author = _field(embed, "author")
            for value in (_field(author, "name") if author else None,
                          _field(embed, "title"),
                          _field(embed, "description")):
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
            text = " - ".join(parts)
    if not text:
        found: list = []
        _component_texts(getattr(message, "components", None), found)
        text = " ".join(found)
    return strip_markdown(text).strip()


def fan_out_discord_message(clan_slug: str, sender: str, content: str,
                            from_bot: bool = False) -> int:
    """Push one Discord line to every present clan member's plugin inbox.
    Returns inboxes pushed; 0 when nobody's plugin is online (the message
    simply doesn't reach the game — there is no backfill, like real chat).
    ``from_bot`` marks lines from an allowlisted bot or webhook; builds that
    don't know the flag ignore it."""
    message = sanitize_discord_content(content)
    if not message:
        return 0
    from services.plugin_notifications import build_envelope, push_to_inbox

    payload = {"sender": str(sender or "Discord")[:32], "message": message}
    if from_bot:
        payload["bot"] = True
    envelope = build_envelope(ENVELOPE_TYPE, payload)
    delivered = 0
    for player_id in online_player_ids(clan_slug):
        if push_to_inbox(player_id, envelope):
            delivered += 1
    return delivered
