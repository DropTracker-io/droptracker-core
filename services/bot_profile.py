"""Custom bot profile: the core bot's nickname, avatar, banner and bio per server.

Discord lets an application set its own member profile per guild with
``PATCH /guilds/{guild_id}/members/@me`` (``nick``, ``avatar``, ``banner``,
``bio``). Every process posting as the core bot (notifications, events,
lootboards, recaps, slash replies) then shows that look in that server, with no
change to any send path.

How it fits together:

* The Web API (web_api/routes/bot_profile.py) validates and stores what the
  group asked for in ``group_bot_profiles`` and calls :func:`request_sync`.
* The core bot runs :func:`drain_requests` every few seconds and
  :func:`sweep` every few minutes. Both end in :func:`reconcile_group`, which
  diffs the desired state against what it last applied and PATCHes only the
  difference.
* Desired state is "nothing" when the group no longer holds the
  ``custom_bot_profile`` entitlement, so a lapse reverts the bot to its default
  look. Every subscription change already goes through
  ``db.entitlements.invalidate_entitlement_cache``, which requests a sync, so
  that happens within seconds; the sweep catches a plain date expiry.
* Config wins over a hand edit: the sweep reads the bot's member back and puts
  our nickname back if a server admin changed it. Admins cannot change the
  bot's avatar, banner or bio, so only the nickname is read back. A nickname we
  never set is left alone.

Never use interactions.py's ``http.modify_current_member``: it only knows
``nick`` and sends ``nick: null`` when called without one, which wipes the
nickname (the same trap as ``Role.edit``). This module sends a raw PATCH with
only the changed keys.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

NICK_MAX = 32
# Discord's limit for a member bio. A custom bio always ends with
# BIO_SIGNATURE (added when the bot applies it, see with_signature), so the
# group gets what is left. The bot's own default bio never gets it.
DISCORD_BIO_MAX = 190
BIO_SIGNATURE = "Powered by The DropTracker\nhttps://www.droptracker.io"
_SIGNATURE_SEP = "\n\n"
BIO_MAX = DISCORD_BIO_MAX - len(_SIGNATURE_SEP) - len(BIO_SIGNATURE)
BIO_MAX_LINES = 6

# Sizes the bot's images are stored and sent at. Discord shows member banners
# at 5:2, so the banner is re-encoded to that shape.
AVATAR_SIZE = (512, 512)
BANNER_SIZE = (960, 384)
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_MAX_PIXELS = 40_000_000
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")
# Discord rate-limits avatar and banner changes, so one upload per image kind
# per group per this many seconds.
IMAGE_COOLDOWN_SECONDS = 600

STATUSES = ("pending", "applied", "default", "error")

# Fields as stored -> the key Discord's PATCH takes.
TEXT_FIELDS = {"nick": "nick", "bio": "bio"}
IMAGE_FIELDS = {"avatar_url": "avatar", "banner_url": "banner"}
ALL_FIELDS = ("nick", "bio", "avatar_url", "banner_url")

SYNC_SET_KEY = "bot_profile:sync"
SYNC_ALL_KEY = "bot_profile:sync_all"

# A request that waits longer than this (interactions sleeps through a 429
# inside the call) is abandoned and retried after RATE_LIMIT_BACKOFF.
REQUEST_TIMEOUT = 30
RATE_LIMIT_BACKOFF = timedelta(minutes=10)
# 403/404 need someone to fix permissions or re-invite the bot; retry slowly.
ACCESS_BACKOFF = timedelta(minutes=30)
BAD_REQUEST_BACKOFF = timedelta(hours=1)

# Names that would pass the bot off as Discord, us, or a service we integrate
# with. Matched against a squashed form (see _squash), so "Drop Tracker",
# "D1scord" and "wise-old-man" all match.
BLOCKED_NAME_TERMS = (
    "discord",
    "clyde",
    "droptracker",
    "wiseoldman",
    "runelite",
    "jagex",
)
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
                       "$": "s", "@": "a", "|": "i", "!": "i"})


class ProfileValidationError(ValueError):
    """The group's input can't be used. The message is shown to the user."""


# --------------------------------------------------------------------------- #
# Validation (Web API side)
# --------------------------------------------------------------------------- #
def _squash(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold().translate(_LEET)
    return re.sub(r"[^a-z]", "", folded)


def blocked_term(text: Optional[str]) -> Optional[str]:
    """The blocked term ``text`` contains, or None."""
    if not text:
        return None
    squashed = _squash(text)
    for term in BLOCKED_NAME_TERMS:
        if term in squashed:
            return term
    return None


def clean_nick(value: Any) -> Optional[str]:
    """A usable nickname, or None to use the bot's normal name."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileValidationError("The nickname must be text.")
    nick = re.sub(r"\s+", " ", value).strip()
    if not nick:
        return None
    if len(nick) > NICK_MAX:
        raise ProfileValidationError(f"The nickname can be at most {NICK_MAX} characters.")
    term = blocked_term(nick)
    if term:
        raise ProfileValidationError(
            "That nickname could be mistaken for Discord or another service. Pick a different one."
        )
    return nick


def clean_bio(value: Any) -> Optional[str]:
    """A usable bio, or None for no bio."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProfileValidationError("The bio must be text.")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.replace("\r\n", "\n").split("\n")]
    bio = "\n".join(lines).strip()
    bio = re.sub(r"\n{3,}", "\n\n", bio)
    if not bio:
        return None
    if len(bio) > BIO_MAX:
        raise ProfileValidationError(f"The bio can be at most {BIO_MAX} characters.")
    if bio.count("\n") + 1 > BIO_MAX_LINES:
        raise ProfileValidationError(f"The bio can be at most {BIO_MAX_LINES} lines.")
    return bio


def process_image(raw: bytes, kind: str) -> bytes:
    """Validate an upload and re-encode it as a PNG at the bot's size.

    ``kind`` is ``avatar`` or ``banner``. Center-crops to the target shape.
    Animated images keep their first frame.
    """
    from PIL import Image, UnidentifiedImageError

    size = AVATAR_SIZE if kind == "avatar" else BANNER_SIZE
    if not raw:
        raise ProfileValidationError("The uploaded image was empty.")
    if len(raw) > IMAGE_MAX_BYTES:
        raise ProfileValidationError("Images are capped at 8 MB.")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = probe.format
        width, height = probe.size
        probe.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ProfileValidationError("Upload a PNG, JPEG or WebP image.")
    if fmt not in IMAGE_FORMATS:
        raise ProfileValidationError("Upload a PNG, JPEG or WebP image.")
    if width * height > IMAGE_MAX_PIXELS:
        raise ProfileValidationError("That image is too large. Use one under 40 megapixels.")

    im = Image.open(io.BytesIO(raw))
    im.seek(0)
    im = im.convert("RGBA")
    target_w, target_h = size
    scale = max(target_w / im.width, target_h / im.height)
    resized = im.resize((max(target_w, round(im.width * scale)), max(target_h, round(im.height * scale))),
                        Image.LANCZOS)
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    cropped = resized.crop((left, top, left + target_w, top + target_h))
    out = io.BytesIO()
    cropped.save(out, format="PNG", optimize=True)
    return out.getvalue()


def image_key(group_id: int, kind: str) -> str:
    return f"dt_uploads/bot_profiles/{int(group_id)}-{kind}-{uuid.uuid4().hex[:12]}.png"


def public_url(key: str) -> str:
    base = os.getenv("B2_CDN_BASE_URL", "https://videos.droptracker.io")
    return f"{base.rstrip('/')}/{key}"


def _redis():
    try:
        from utils.redis import redis_client

        return getattr(redis_client, "client", None)
    except Exception:
        return None


def claim_image_slot(group_id: int, kind: str) -> int:
    """0 if the group may change this image now (and starts its cooldown),
    else the seconds left. Fails open when Redis is down; Discord still
    enforces its own limit."""
    conn = _redis()
    if conn is None:
        return 0
    key = f"bot_profile:cooldown:{int(group_id)}:{kind}"
    try:
        if conn.set(key, "1", nx=True, ex=IMAGE_COOLDOWN_SECONDS):
            return 0
        ttl = conn.ttl(key)
        return int(ttl) if ttl and ttl > 0 else IMAGE_COOLDOWN_SECONDS
    except Exception:
        return 0


def image_cooldown_left(group_id: int, kind: str) -> int:
    """Seconds until this image can change again, without claiming anything."""
    conn = _redis()
    if conn is None:
        return 0
    try:
        ttl = conn.ttl(f"bot_profile:cooldown:{int(group_id)}:{kind}")
        return int(ttl) if ttl and ttl > 0 else 0
    except Exception:
        return 0


def staged_url_ok(url: str, group_id: int, kind: str) -> bool:
    """Whether ``url`` is an image this group uploaded through our endpoint,
    so a save can't point the bot at an arbitrary address."""
    prefix = public_url(f"dt_uploads/bot_profiles/{int(group_id)}-{kind}-")
    return isinstance(url, str) and url.startswith(prefix) and url.endswith(".png") and ".." not in url


def release_image_slot(group_id: int, kind: str) -> None:
    """Give the cooldown back after an upload that failed before saving."""
    conn = _redis()
    if conn is None:
        return
    try:
        conn.delete(f"bot_profile:cooldown:{int(group_id)}:{kind}")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# The bot's own default look
# --------------------------------------------------------------------------- #
# What a server sees with no customization: the core bot's global profile.
# The core bot reads it from Discord and caches it here; the Web API only
# reads Redis (it never talks to Discord itself).
DEFAULTS_KEY = "bot_profile:defaults"
DEFAULTS_MAX_AGE = 3600
FALLBACK_DEFAULTS = {"name": "DropTracker", "avatar_url": None, "banner_url": None, "bio": None}


def build_defaults(user: Dict[str, Any], app: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Default look from Discord's ``/users/@me`` and ``/applications/@me``.
    A bot's profile "About me" is its application description."""
    uid = user.get("id")
    avatar = user.get("avatar")
    banner = user.get("banner")
    cdn = "https://cdn.discordapp.com"

    def asset(path: str, h: Optional[str], size: int) -> Optional[str]:
        if not (uid and h):
            return None
        ext = "gif" if h.startswith("a_") else "png"
        return f"{cdn}/{path}/{uid}/{h}.{ext}?size={size}"

    return {
        "name": user.get("global_name") or user.get("username") or FALLBACK_DEFAULTS["name"],
        "avatar_url": asset("avatars", avatar, 256),
        "banner_url": asset("banners", banner, 1024),
        "bio": ((app or {}).get("description") or "").strip() or None,
    }


def load_defaults() -> Dict[str, Any]:
    conn = _redis()
    if conn is not None:
        try:
            import json

            raw = conn.get(DEFAULTS_KEY)
            if raw:
                data = json.loads(raw)
                return {
                    "name": data.get("name") or FALLBACK_DEFAULTS["name"],
                    "avatar_url": data.get("avatar_url"),
                    "banner_url": data.get("banner_url"),
                    "bio": data.get("bio"),
                }
        except Exception:
            pass
    return dict(FALLBACK_DEFAULTS)


def _defaults_stale() -> bool:
    conn = _redis()
    if conn is None:
        return False
    try:
        import json

        raw = conn.get(DEFAULTS_KEY)
        if not raw:
            return True
        return (datetime.now().timestamp() - float(json.loads(raw).get("fetched_at") or 0)) > DEFAULTS_MAX_AGE
    except Exception:
        return True


async def refresh_defaults(bot, *, force: bool = False) -> None:
    """Cache the core bot's global profile for the Web API. Never raises."""
    if not force and not _defaults_stale():
        return
    try:
        import json

        from interactions.api.http.route import Route

        user = await asyncio.wait_for(bot.http.request(Route("GET", "/users/@me")), timeout=REQUEST_TIMEOUT)
        try:
            app = await asyncio.wait_for(bot.http.request(Route("GET", "/applications/@me")), timeout=REQUEST_TIMEOUT)
        except Exception:
            app = None
        if not isinstance(user, dict):
            return
        data = build_defaults(user, app if isinstance(app, dict) else None)
        data["fetched_at"] = datetime.now().timestamp()
        conn = _redis()
        if conn is not None:
            conn.set(DEFAULTS_KEY, json.dumps(data))
    except Exception as e:
        print(f"[bot_profile] default profile refresh failed: {e}")


# --------------------------------------------------------------------------- #
# Sync requests
# --------------------------------------------------------------------------- #
def request_sync(group_id: Optional[int] = None) -> bool:
    """Ask the core bot to reconcile one group (or every group, for None) on
    its next poll. Best-effort; never raises."""
    conn = _redis()
    if conn is None:
        return False
    try:
        if group_id is None:
            conn.set(SYNC_ALL_KEY, "1")
        else:
            conn.sadd(SYNC_SET_KEY, int(group_id))
        return True
    except Exception:
        return False


def consume_requests() -> Tuple[bool, List[int]]:
    """(everyone requested, group ids requested), clearing both."""
    conn = _redis()
    if conn is None:
        return False, []
    try:
        pipe = conn.pipeline()
        pipe.getdel(SYNC_ALL_KEY)
        pipe.smembers(SYNC_SET_KEY)
        pipe.delete(SYNC_SET_KEY)
        all_flag, members, _ = pipe.execute()
    except Exception:
        return False, []
    ids = []
    for m in members or ():
        try:
            ids.append(int(m.decode() if isinstance(m, (bytes, bytearray)) else m))
        except (TypeError, ValueError):
            continue
    return bool(all_flag), sorted(set(ids))


# --------------------------------------------------------------------------- #
# Planning (pure)
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    group_id: int
    group_name: str
    guild_id: Optional[str]          # the group's server now
    applied_guild_id: Optional[str]  # where the applied_* state lives
    # None when entitlement resolution failed: never act on a guess.
    entitled: Optional[bool]
    desired: Dict[str, Optional[str]] = field(default_factory=dict)
    applied: Dict[str, Optional[str]] = field(default_factory=dict)
    retry_after: Optional[datetime] = None
    updated_by_user_id: Optional[int] = None


def with_signature(bio: Optional[str]) -> Optional[str]:
    """A custom bio as Discord shows it: the group's text, then the fixed
    "Powered by" signature. None stays None (the bot's own bio shows)."""
    if not bio:
        return None
    room = BIO_MAX
    text = bio.strip()
    if len(text) > room:  # saved before the signature existed
        text = text[: room - 3].rstrip() + "..."
    return f"{text}{_SIGNATURE_SEP}{BIO_SIGNATURE}"


def target_state(snap: Snapshot) -> Dict[str, Optional[str]]:
    """What the bot should look like: the group's settings when entitled,
    otherwise Discord's default (every field None). The bio carries the
    signature here, so it is compared with what was applied: a bio applied
    before the signature existed differs and gets re-sent by the sweep."""
    if not snap.entitled:
        return {f: None for f in ALL_FIELDS}
    target = {f: snap.desired.get(f) for f in ALL_FIELDS}
    target["bio"] = with_signature(target["bio"])
    return target


def plan_changes(target: Dict[str, Optional[str]], applied: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    """Stored-field -> new value, for fields that differ. Empty means nothing to do."""
    return {f: target.get(f) for f in ALL_FIELDS if (target.get(f) or None) != (applied.get(f) or None)}


def split_requests(changes: Dict[str, Optional[str]]) -> List[Dict[str, Optional[str]]]:
    """The nickname goes alone: it needs Change Nickname, and a 403 for it
    must not block the avatar, banner or bio."""
    batches = []
    if "nick" in changes:
        batches.append({"nick": changes["nick"]})
    rest = {k: v for k, v in changes.items() if k != "nick"}
    if rest:
        batches.append(rest)
    return batches


def discord_payload(batch: Dict[str, Optional[str]], images: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    """Discord's PATCH body for a batch. ``images`` maps a stored image URL to
    its data URI. Only the keys in the batch are sent."""
    payload: Dict[str, Optional[str]] = {}
    for stored, value in batch.items():
        if stored in TEXT_FIELDS:
            payload[TEXT_FIELDS[stored]] = value or None
        elif stored in IMAGE_FIELDS:
            payload[IMAGE_FIELDS[stored]] = images.get(value) if value else None
    return payload


def classify_error(exc: BaseException, batch: Dict[str, Optional[str]]) -> Tuple[str, timedelta]:
    """(message for the group's settings page, how long to wait before retrying)."""
    status = getattr(exc, "status", None)
    if isinstance(exc, asyncio.TimeoutError) or status == 429:
        return "Discord is rate limiting changes. Trying again shortly.", RATE_LIMIT_BACKOFF
    if status == 403:
        if "nick" in batch:
            return ("The bot needs the Change Nickname permission in your server to use a nickname.",
                    ACCESS_BACKOFF)
        return "Discord refused the change. Check the bot is still in your server.", ACCESS_BACKOFF
    if status == 404:
        return "The bot is not in your Discord server. Invite it again to apply these settings.", ACCESS_BACKOFF
    if status == 400:
        text = str(getattr(exc, "text", "") or exc)
        if "fast" in text.lower() or "rate" in text.lower():
            return "Discord says the image is changing too often. Trying again later.", RATE_LIMIT_BACKOFF
        return f"Discord rejected the change: {text[:180]}", BAD_REQUEST_BACKOFF
    return f"Couldn't reach Discord: {str(exc)[:180]}", RATE_LIMIT_BACKOFF


def status_for(target: Dict[str, Optional[str]]) -> str:
    return "applied" if any(target.get(f) for f in ALL_FIELDS) else "default"


# --------------------------------------------------------------------------- #
# DB access (sync; called through asyncio.to_thread)
# --------------------------------------------------------------------------- #
def _resolve_entitled(s, group_id: int) -> Optional[bool]:
    """Uncached and NOT fail-closed: ``group_has_entitlement`` answers False on
    any error, which here would revert a paying group's bot."""
    try:
        from db.entitlements import resolve_group_entitlements

        return bool(resolve_group_entitlements(s, group_id).get("custom_bot_profile"))
    except Exception as e:
        print(f"[bot_profile] entitlement lookup failed for group {group_id}: {e}")
        return None


def load_snapshot(group_id: int) -> Optional[Snapshot]:
    from db.models import Group, GroupBotProfile, Session

    with Session() as s:
        row = s.get(GroupBotProfile, group_id)
        if row is None:
            return None
        group = s.get(Group, group_id)
        entitled = _resolve_entitled(s, group_id) if group is not None else False
        return Snapshot(
            group_id=group_id,
            group_name=getattr(group, "group_name", None) or f"Group {group_id}",
            guild_id=str(group.guild_id) if group is not None and group.guild_id else None,
            applied_guild_id=row.guild_id,
            entitled=entitled,
            desired={"nick": row.nick, "bio": row.bio,
                     "avatar_url": row.avatar_url, "banner_url": row.banner_url},
            applied={"nick": row.applied_nick, "bio": row.applied_bio,
                     "avatar_url": row.applied_avatar_url, "banner_url": row.applied_banner_url},
            retry_after=row.retry_after,
            updated_by_user_id=row.updated_by_user_id,
        )


def sweep_group_ids() -> List[int]:
    """Every group with a profile row. The table only holds groups that ever
    used the feature, so this stays small."""
    from db.models import GroupBotProfile, Session

    with Session() as s:
        return [gid for (gid,) in s.query(GroupBotProfile.group_id).all()]


_APPLIED_COLUMN = {"nick": "applied_nick", "bio": "applied_bio",
                   "avatar_url": "applied_avatar_url", "banner_url": "applied_banner_url"}


def record_result(group_id: int, guild_id: Optional[str], sent: Dict[str, Optional[str]],
                  target: Dict[str, Optional[str]], error: Optional[str] = None,
                  backoff: Optional[timedelta] = None, reset_applied: bool = False) -> None:
    """Write what reached Discord. ``sent`` holds only fields Discord accepted."""
    from db.models import GroupBotProfile, Session

    with Session() as s:
        row = s.get(GroupBotProfile, group_id)
        if row is None:
            return
        if reset_applied:
            for col in _APPLIED_COLUMN.values():
                setattr(row, col, None)
        for stored, value in sent.items():
            setattr(row, _APPLIED_COLUMN[stored], value)
        if sent or reset_applied:
            row.applied_at = datetime.now()
        row.guild_id = guild_id
        if error:
            row.status = "error"
            row.last_error = error[:255]
            row.retry_after = datetime.now() + (backoff or RATE_LIMIT_BACKOFF)
        else:
            row.status = status_for(target)
            row.last_error = None
            row.retry_after = None
        s.commit()


def record_audit(group_id: int, action: str, before: Dict[str, Any], after: Dict[str, Any],
                 actor_user_id: Optional[int] = None) -> None:
    import json

    from db.models import AuditLog, Session

    try:
        with Session() as s:
            s.add(AuditLog(actor_user_id=actor_user_id, group_id=group_id, action=action,
                           target="group_bot_profiles", before=json.dumps(before), after=json.dumps(after)))
            s.commit()
    except Exception as e:
        print(f"[bot_profile] audit write failed for group {group_id}: {e}")


# --------------------------------------------------------------------------- #
# Discord I/O (core bot)
# --------------------------------------------------------------------------- #
async def _fetch_data_uri(url: str) -> str:
    import aiohttp

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            body = await resp.read()
    if len(body) > IMAGE_MAX_BYTES:
        raise ValueError("stored image is larger than expected")
    return "data:image/png;base64," + base64.b64encode(body).decode()


async def _patch_member(bot, guild_id: str, payload: Dict[str, Optional[str]], reason: str):
    from interactions.api.http.route import Route

    route = Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=int(guild_id))
    result = await asyncio.wait_for(bot.http.request(route, payload=payload, reason=reason),
                                    timeout=REQUEST_TIMEOUT)
    if result is None:
        # interactions returns None after exhausting its 429 retries.
        raise asyncio.TimeoutError()
    return result


async def _current_nick(bot, guild_id: str) -> Tuple[bool, Optional[str]]:
    """(read ok, the bot's nickname in the guild)."""
    try:
        member = await asyncio.wait_for(bot.http.get_member(int(guild_id), int(bot.user.id)),
                                        timeout=REQUEST_TIMEOUT)
        return True, (member or {}).get("nick")
    except Exception:
        return False, None


def _guild_allowed(guild_id: str) -> bool:
    try:
        from utils import dev_guild_guard

        return not dev_guild_guard.is_active() or dev_guild_guard.guild_allowed(guild_id)
    except Exception:
        return True


async def _apply(bot, guild_id: str, changes: Dict[str, Optional[str]], reason: str
                 ) -> Tuple[Dict[str, Optional[str]], Optional[str], Optional[timedelta]]:
    """Send ``changes`` to one guild. Returns (fields Discord accepted, error, backoff).

    A failed nickname batch (usually a missing Change Nickname permission)
    still lets the avatar, banner and bio through; any other failure stops
    the pass so a rate limit isn't hammered."""
    sent: Dict[str, Optional[str]] = {}
    error: Optional[str] = None
    backoff: Optional[timedelta] = None
    for batch in split_requests(changes):
        try:
            images = {}
            for stored in IMAGE_FIELDS:
                url = batch.get(stored)
                if url:
                    images[url] = await _fetch_data_uri(url)
            await _patch_member(bot, guild_id, discord_payload(batch, images), reason)
            sent.update(batch)
        except Exception as e:
            error, backoff = classify_error(e, batch)
            if "nick" not in batch:
                break
    return sent, error, backoff


_lock = asyncio.Lock()


async def reconcile_group(bot, group_id: int, *, check_drift: bool = False, force: bool = False) -> Optional[str]:
    """Bring one group's bot profile in line. Returns what happened, for logs."""
    snap = await asyncio.to_thread(load_snapshot, group_id)
    if snap is None or snap.entitled is None:
        return None
    if not force and snap.retry_after and snap.retry_after > datetime.now():
        return None

    target = target_state(snap)
    applied = dict(snap.applied)
    reset_applied = False

    # The group moved servers: put the old server's bot back to default first.
    if snap.applied_guild_id and snap.guild_id != snap.applied_guild_id:
        old = plan_changes({f: None for f in ALL_FIELDS}, applied)
        if old and _guild_allowed(snap.applied_guild_id):
            await _apply(bot, snap.applied_guild_id, old, "DropTracker: group moved servers")
        applied = {f: None for f in ALL_FIELDS}
        reset_applied = True

    if not snap.guild_id:
        if reset_applied:
            await asyncio.to_thread(record_result, group_id, None, {}, target, None, None, True)
        return None
    if not _guild_allowed(snap.guild_id):
        return None

    if check_drift and applied.get("nick"):
        ok, live_nick = await _current_nick(bot, snap.guild_id)
        if ok and (live_nick or None) != applied.get("nick"):
            applied["nick"] = live_nick

    changes = plan_changes(target, applied)
    if not changes:
        if reset_applied:
            await asyncio.to_thread(record_result, group_id, snap.guild_id, {}, target, None, None, True)
        return None

    lapsed = not snap.entitled and any(snap.desired.get(f) for f in ALL_FIELDS)
    reason = "DropTracker: subscription ended" if lapsed else "DropTracker: group bot settings"
    sent, error, backoff = await _apply(bot, snap.guild_id, changes, reason)
    await asyncio.to_thread(record_result, group_id, snap.guild_id, sent, target, error, backoff, reset_applied)

    if sent:
        before = {f: applied.get(f) for f in sent}
        if lapsed:
            await asyncio.to_thread(record_audit, group_id, "bot_profile.lapse_revert", before, sent)
        await post_staff_feed(bot, snap, before, sent, lapsed=lapsed)
    return f"group {group_id}: sent {sorted(sent)}" + (f", error: {error}" if error else "")


async def drain_requests(bot) -> None:
    await refresh_defaults(bot)  # no-op unless the cached default look is missing or an hour old
    everyone, ids = consume_requests()
    if everyone:
        ids = sorted(set(ids) | set(await asyncio.to_thread(sweep_group_ids)))
    if not ids:
        return
    async with _lock:
        for gid in ids:
            try:
                # A save is a fresh instruction: try it even inside a backoff.
                result = await reconcile_group(bot, gid, force=not everyone)
                if result:
                    print(f"[bot_profile] {result}")
            except Exception as e:
                print(f"[bot_profile] reconcile failed for group {gid}: {e}")


async def sweep(bot) -> None:
    ids = await asyncio.to_thread(sweep_group_ids)
    async with _lock:
        for gid in ids:
            try:
                result = await reconcile_group(bot, gid, check_drift=True)
                if result:
                    print(f"[bot_profile] {result}")
            except Exception as e:
                print(f"[bot_profile] sweep failed for group {gid}: {e}")


# --------------------------------------------------------------------------- #
# Staff feed
# --------------------------------------------------------------------------- #
_LABELS = {"nick": "Nickname", "bio": "Bio", "avatar_url": "Avatar", "banner_url": "Banner"}


def _show(field_name: str, value: Optional[str]) -> str:
    if not value:
        return "*default*"
    if field_name in IMAGE_FIELDS:
        return f"[image]({value})"
    return value if len(value) <= 300 else value[:297] + "..."


def staff_feed_embed(snap: Snapshot, before: Dict[str, Optional[str]], after: Dict[str, Optional[str]],
                     *, lapsed: bool, actor: Optional[str] = None) -> Dict[str, Any]:
    if lapsed:
        title = "Bot profile reverted: subscription ended"
    elif not any(after.values()):
        title = "Bot profile reset to default"
    else:
        title = "Bot profile updated"
    fields = [{"name": "Group", "value": f"{snap.group_name} (#{snap.group_id})", "inline": True},
              {"name": "Server", "value": str(snap.guild_id or "-"), "inline": True}]
    if actor and not lapsed:
        fields.append({"name": "Changed by", "value": actor, "inline": True})
    for f in ALL_FIELDS:
        if f in after:
            fields.append({"name": _LABELS[f], "value": f"{_show(f, before.get(f))} → {_show(f, after.get(f))}",
                           "inline": False})
    embed: Dict[str, Any] = {"title": title, "color": 0x8B0000 if lapsed else 0xD4AF37, "fields": fields,
                             "url": f"https://www.droptracker.io/admin/bot-profiles#g{snap.group_id}"}
    if after.get("avatar_url"):
        embed["thumbnail"] = {"url": after["avatar_url"]}
    if after.get("banner_url"):
        embed["image"] = {"url": after["banner_url"]}
    return embed


def _actor_name(user_id: Optional[int]) -> Optional[str]:
    if user_id is None:
        return None
    try:
        from db.models import Session, User

        with Session() as s:
            user = s.get(User, user_id)
            name = getattr(user, "username", None) if user else None
            return f"{name} (user #{user_id})" if name else f"user #{user_id}"
    except Exception:
        return f"user #{user_id}"


async def post_staff_feed(bot, snap: Snapshot, before, after, *, lapsed: bool) -> None:
    """Best-effort post to the staff channel. Never raises."""
    channel_id = os.getenv("STAFF_BOT_PROFILE_CHANNEL_ID", "").strip()
    if not channel_id:
        return
    try:
        actor = await asyncio.to_thread(_actor_name, snap.updated_by_user_id)
        embed = staff_feed_embed(snap, before, after, lapsed=lapsed, actor=actor)
        await asyncio.wait_for(bot.http.create_message({"embeds": [embed]}, int(channel_id)),
                               timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"[bot_profile] staff feed post failed: {e}")
