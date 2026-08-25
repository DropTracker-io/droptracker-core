"""Confine a dev instance's outbound Discord calls to the guilds it serves.

The dev box runs on a scrubbed copy of production, so its database holds ~262
real ``groups.guild_id`` values and ~1,600 real channel ids that the dev
application is not a member of. Every periodic sweep re-resolves all of them,
which burned ~72k failed 403/404 calls a day against the dev app, forever.
Neutralising the ids in the database was the alternative, but that is a
destructive mass UPDATE against the very realism the dev dataset exists for.

This guard instead wraps the shared client's two resolution methods, so an id
belonging to a guild this instance does not serve is answered locally with
"not found" instead of a REST round-trip.

**Inert in production by construction.** It only arms when the process is in
dev mode *and* ``DEV_ALLOWED_GUILDS`` is set; production sets neither, so
``install()`` is a no-op there and every call keeps its stock behaviour.

Two layers are needed, because a channel id does not carry its guild:

* **Guilds** are matched against the allowlist and refused with no request at
  all. This is what does most of the work: the per-guild sweeps resolve the
  guild first and skip the whole body when it comes back empty, so blocking
  one guild also skips its channel enumeration, its role fetch and its
  scheduled-event reconcile.
* **Channels** can only be attributed after the fact, so the first fetch of an
  unknown channel is allowed through and the answer is remembered. Both
  inaccessible outcomes are learned: a 404 raises ``NotFound``, while a 403
  does *not* — ``smart_cache.fetch_channel`` swallows ``Forbidden`` and
  returns a typeless ``BaseChannel`` stub that it never caches, which is
  precisely why the same channel was re-fetched every cycle. A channel that
  does resolve is attributed by its own ``guild_id`` and blocked from then on
  if that guild is not ours.

The negative cache is the shape ``fetch_guild_cached`` in ``bots/main.py``
already uses for dead guilds: a TTL'd Redis marker, so recovery is automatic
and a restart does not re-learn from scratch.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterable, Set

try:  # pragma: no cover - exercised implicitly wherever the bot runs
    from interactions.models.discord.channel import BaseChannel as _BaseChannel
except Exception:  # the library is stubbed out in parts of the test suite
    _BaseChannel = None

#: How long an inaccessible channel stays blocked before it is re-probed.
#: Matches the 6h `_DEAD_GUILD_TTL` in bots/main.py.
DEAD_CHANNEL_TTL = 6 * 3600

#: Env var holding the guild ids this instance is allowed to talk to. Accepts
#: a JSON array (matching TARGET_GUILDS' existing shape) or a bare
#: comma-separated list, so either spelling in a .env works.
ALLOWLIST_ENV = "DEV_ALLOWED_GUILDS"

#: Set when Redis is unreachable, so the guard still works (per-process,
#: re-learned on restart) rather than falling back to hammering Discord.
_local_dead_channels: dict = {}

_blocked_counts = {"guild": 0, "channel": 0}


def _env_flag(name: str) -> str:
    """Read an env var tolerantly of the quoting used in this repo's .env.

    Values there are frequently written ``STATE="live"``, which a bare
    ``os.getenv(...) == "dev"`` comparison silently gets wrong.
    """
    return (os.getenv(name) or "").strip().strip('"').strip("'").lower()


def is_dev_mode() -> bool:
    return _env_flag("STATE") == "dev" or _env_flag("STATUS") == "dev"


def allowed_guild_ids() -> Set[int]:
    """The guild ids this instance may contact, as ints.

    Deliberately re-read per call rather than cached at import: the guard is
    cheap, and a cached value would make the allowlist untestable and
    un-tweakable without a restart.
    """
    raw = (os.getenv(ALLOWLIST_ENV) or "").strip()
    if not raw:
        return set()

    parts: Iterable[str]
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return set()
        parts = [str(p) for p in parsed] if isinstance(parsed, list) else []
    else:
        parts = raw.replace(";", ",").split(",")

    ids = set()
    for part in parts:
        part = str(part).strip().strip('"').strip("'")
        if not part:
            continue
        try:
            ids.add(int(part))
        except (TypeError, ValueError):
            continue
    return ids


def is_active() -> bool:
    """True only when this process should confine its Discord calls.

    Requires BOTH dev mode and a non-empty allowlist. An empty allowlist means
    "not configured", never "talk to nobody" — a guard that silently muted a
    misconfigured production bot would be far worse than the traffic it saves.
    """
    return is_dev_mode() and bool(allowed_guild_ids())


def guild_allowed(guild_id) -> bool:
    """Whether `guild_id` is one this instance serves. Unparseable ids are
    allowed through — the guard's job is to block ids it can positively
    attribute elsewhere, not to swallow malformed data that a caller should
    still see fail."""
    try:
        return int(guild_id) in allowed_guild_ids()
    except (TypeError, ValueError):
        return True


def blocked_counts() -> dict:
    """Copy of the per-process block tallies, for logging/tests."""
    return dict(_blocked_counts)


# --- channel negative cache --------------------------------------------------

def _dead_key(channel_id) -> str:
    return f"channel:dead:{channel_id}"


def _redis():
    """The shared Redis client, or None when it is unavailable."""
    try:
        from utils.redis import redis_client
        return redis_client
    except Exception:
        return None


def is_channel_known_dead(channel_id) -> bool:
    client = _redis()
    if client is not None:
        try:
            if client.get(_dead_key(channel_id)):
                return True
        except Exception:
            pass
    expires = _local_dead_channels.get(str(channel_id))
    return expires is not None and expires > time.monotonic()


def mark_channel_dead(channel_id) -> None:
    _local_dead_channels[str(channel_id)] = time.monotonic() + DEAD_CHANNEL_TTL
    client = _redis()
    if client is None:
        return
    try:
        client.setex(_dead_key(channel_id), DEAD_CHANNEL_TTL, "1")
    except Exception:
        pass


def _looks_inaccessible(channel) -> bool:
    """Is this the stub `smart_cache.fetch_channel` hands back on a 403?

    That path builds ``BaseChannel.from_dict({"id": ..., "type": MISSING})``,
    and the discriminator has to be the *class*: the stub is a bare
    ``BaseChannel`` while everything that really resolved is one of its
    subclasses (GuildText, GuildForum, DM, …).

    Two tempting checks are both wrong and were verified against the installed
    library before settling here. ``channel.type is MISSING`` is False — the
    sentinel is coerced into a ``ChannelType`` on the way in. And a truthiness
    test on ``channel.type`` would be a disaster: ``ChannelType.GUILD_TEXT``
    is 0, so ``not channel.type`` is True for an ordinary text channel and the
    guard would silently swallow every real notification.
    """
    if channel is None:
        return True
    return _BaseChannel is not None and type(channel) is _BaseChannel


def _channel_guild_id(channel):
    """The guild a resolved channel belongs to, or None for DMs/stubs.

    Guild channels store this as the private ``_guild_id`` — there is no
    public ``guild_id`` attribute, only a ``.guild`` property that would hit
    the cache. The public name is still tried second in case a later version
    adds one.
    """
    for attr in ("_guild_id", "guild_id"):
        value = getattr(channel, attr, None)
        if value is not None:
            return value
    return None


def install(bot) -> bool:
    """Wrap `bot.fetch_guild` / `bot.fetch_channel` in place.

    Returns True when the guard was armed. Idempotent — installing twice would
    otherwise stack wrappers and double-count every block.
    """
    if not is_active():
        return False
    if getattr(bot, "_dev_guild_guard_installed", False):
        return True

    original_fetch_guild = bot.fetch_guild
    original_fetch_channel = bot.fetch_channel

    async def guarded_fetch_guild(guild_id, *args, **kwargs):
        if not guild_allowed(guild_id):
            _blocked_counts["guild"] += 1
            # None is fetch_guild's own "not a member" answer (it returns None
            # on 404), so every existing caller already handles this.
            return None
        return await original_fetch_guild(guild_id, *args, **kwargs)

    async def guarded_fetch_channel(channel_id, *args, **kwargs):
        if is_channel_known_dead(channel_id):
            _blocked_counts["channel"] += 1
            return None

        try:
            channel = await original_fetch_channel(channel_id, *args, **kwargs)
        except Exception as e:
            # 404 propagates from the library (only Forbidden is swallowed).
            # Remember it, then let the caller see the error it expects.
            if getattr(e, "status", None) in (403, 404):
                mark_channel_dead(channel_id)
            raise

        if _looks_inaccessible(channel):
            mark_channel_dead(channel_id)
            return None

        # Resolved: attribute it. A channel in a guild we do not serve can be
        # blocked outright from here on, which is what actually drains the
        # sweeps — the stub above is never cached by the library, so without
        # this the same channel is re-fetched every single cycle.
        guild_id = _channel_guild_id(channel)
        if guild_id is not None and not guild_allowed(guild_id):
            mark_channel_dead(channel_id)
            return None

        return channel

    bot.fetch_guild = guarded_fetch_guild
    bot.fetch_channel = guarded_fetch_channel
    bot._dev_guild_guard_installed = True
    return True


def describe() -> str:
    """One-line summary for a startup log line."""
    if not is_dev_mode():
        return "dev guild guard: inactive (not a dev instance)"
    ids = allowed_guild_ids()
    if not ids:
        return (f"dev guild guard: INACTIVE — dev mode but {ALLOWLIST_ENV} is unset, "
                f"so Discord calls are unconfined")
    return (f"dev guild guard: active, confined to {sorted(ids)} "
            f"(channels re-probed every {DEAD_CHANNEL_TTL // 3600}h)")
