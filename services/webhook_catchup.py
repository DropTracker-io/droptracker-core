"""Catch the Discord webhook reader up on messages it missed while it was down.

Webhook-only clients (``useApi=false``) post straight to our Discord webhooks.
The reader bot (``bots/webhook_bot.py``) turns each message into submissions
as it arrives, with no queue behind it, so anything posted while the bot was
down (a restart, a crash, the whole box offline) used to stay unprocessed in
the channel until someone ran ``scripts/replay_webhook_window.py`` by hand.

This module makes that automatic:

* **Watermark.** The live listener records the newest message it has seen in
  Redis (:func:`note_seen`, throttled). It survives restarts.
* **Catch-up.** On startup the bot replays every target-guild channel's history
  from just before the watermark up to now (:func:`run_catchup`), through the
  same bundler and dispatcher as the live path. GUID dedup
  (``data.submissions.common.ensure_can_create``, unbounded in time) makes the
  overlap with what the bot already processed a no-op.
* **Original dating.** Each recovered payload is stamped with Discord's message
  time and marked trusted, so rows are dated when the player's client posted
  them (``utils.replay_stamp``), not when the bot came back.
* **Crash safety.** The gap's start is pinned in ``PENDING_KEY`` until a
  catch-up finishes, because the live listener advances the watermark while
  the catch-up is still working through older history. A restart mid-catch-up
  starts again from the pinned point instead of skipping the rest.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from utils.replay_stamp import TRUSTED_FLAG, TRUSTED_RECEIVED_AT_MAX_LAG

WATERMARK_KEY = "webhookbot:watermark"
PENDING_KEY = "webhookbot:catchup_from"

# Start this far before the watermark: it is written at most every
# WATERMARK_WRITE_INTERVAL, and messages can land slightly out of order.
MARGIN = timedelta(minutes=2)
WATERMARK_WRITE_INTERVAL = 5.0
# Past this the stamp would no longer be believed anyway (rows would be dated
# "now"), and a longer silence points at a broken pipeline someone should look at.
MAX_LOOKBACK = TRUSTED_RECEIVED_AT_MAX_LAG
# Yield between messages so a large backlog cannot starve the gateway heartbeat
# (see the 2026-09 gateway-stall incidents).
PER_MESSAGE_YIELD = 0.01

DISCORD_EPOCH_MS = 1420070400000


def snowflake_for(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def snowflake_time(snowflake: int) -> datetime:
    return datetime.fromtimestamp(((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000,
                                  tz=timezone.utc)


def _default_store():
    from utils.redis import redis_client
    return redis_client


def _mark_active(store, after_id) -> None:
    """Tell the events sweep a catch-up is replaying from ``after_id`` (t274:
    scheduled ends inside the gap wait for it). Expires on its own, unlike
    PENDING_KEY, so a crashed catch-up stops holding events within the TTL."""
    from utils.event_end_hold import CATCHUP_ACTIVE_KEY, CATCHUP_ACTIVE_TTL_SECONDS

    try:
        try:
            store.set(CATCHUP_ACTIVE_KEY, str(after_id), ex=CATCHUP_ACTIVE_TTL_SECONDS)
        except TypeError:  # a store without TTL support (tests)
            store.set(CATCHUP_ACTIVE_KEY, str(after_id))
    except Exception:
        pass


def _clear_active(store) -> None:
    from utils.event_end_hold import CATCHUP_ACTIVE_KEY

    try:
        store.delete(CATCHUP_ACTIVE_KEY)
    except Exception:
        pass


def _read_id(store, key) -> Optional[int]:
    raw = store.get(key)
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Live watermark
# --------------------------------------------------------------------------

class Watermark:
    """Newest message id the live listener has seen, persisted with a throttle."""

    def __init__(self, store=None, clock: Callable[[], float] = time.monotonic):
        self._store = store
        self._clock = clock
        self._newest = 0
        self._written = 0
        self._last_write = float("-inf")

    @property
    def store(self):
        if self._store is None:
            self._store = _default_store()
        return self._store

    def note(self, message_id) -> None:
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return
        if message_id > self._newest:
            self._newest = message_id
        if (self._newest > self._written
                and self._clock() - self._last_write >= WATERMARK_WRITE_INTERVAL):
            self.flush()

    def flush(self) -> None:
        if self._newest <= self._written:
            return
        try:
            self.store.set(WATERMARK_KEY, str(self._newest))
            self._written = self._newest
            self._last_write = self._clock()
        except Exception:
            pass


_watermark = Watermark()


def note_seen(message) -> None:
    """Live listener hook: record that this message reached the bot. Never raises."""
    try:
        _watermark.note(message.id)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Catch-up window
# --------------------------------------------------------------------------

def plan_window(watermark_id: Optional[int], pending_id: Optional[int],
                now: datetime) -> Optional[tuple[int, int, bool]]:
    """``(after_id, before_id, clamped)`` to scan, or None when there is no
    reference point yet (first run: nothing to catch up on)."""
    starts = [i for i in (watermark_id, pending_id) if i]
    if not starts:
        return None
    start = snowflake_time(min(starts)) - MARGIN
    floor = now - MAX_LOOKBACK
    clamped = start < floor
    if clamped:
        start = floor
    return snowflake_for(start), snowflake_for(now), clamped


def stamp_bundle(bundle, created_at) -> None:
    """Date each recovered payload when Discord received the message."""
    if created_at is None:
        return
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    stamped = created_at.isoformat()
    for _, embed_data in bundle:
        embed_data["_received_at"] = stamped
        embed_data[TRUSTED_FLAG] = True


def should_replay(message, own_user_id) -> bool:
    """The live listener's own filters, so a catch-up cannot ingest more."""
    author = getattr(message, "author", None)
    if author is None or getattr(author, "system", False):
        return False
    if own_user_id is not None and author.id == own_user_id:
        return False
    return bool(message.embeds)


async def collect_channels(client, guild_ids, only_channels=None, log=print):
    """Text channels the bot can actually read, across the target guilds."""
    from interactions import ChannelType

    channels = []
    for guild_id in guild_ids:
        try:
            guild = await client.fetch_guild(guild_id)
        except Exception as e:
            log(f"  ! guild {guild_id}: cannot fetch ({e})")
            continue
        if guild is None:
            log(f"  ! guild {guild_id}: not found / bot not a member")
            continue
        try:
            found = await guild.fetch_channels()
        except Exception as e:
            log(f"  ! guild {guild_id}: cannot list channels ({e})")
            continue
        for channel in found:
            if channel.type not in (ChannelType.GUILD_TEXT, ChannelType.GUILD_NEWS):
                continue
            if only_channels and str(channel.id) not in only_channels:
                continue
            channels.append((guild, channel))
    return channels


_running = asyncio.Lock()


async def run_catchup(client, guild_ids, build_message_bundle, process_message_bundle,
                      *, store=None, now: Optional[datetime] = None,
                      log=print) -> Optional[dict]:
    """Replay what the reader missed since its watermark. Returns counts, or
    None when skipped (already running, or no watermark yet)."""
    if _running.locked():
        log("[WebhookCatchup] already running; skipping")
        return None
    async with _running:
        store = store if store is not None else _default_store()
        now = now or datetime.now(timezone.utc)
        watermark_id = _read_id(store, WATERMARK_KEY)
        pending_id = _read_id(store, PENDING_KEY)
        window = plan_window(watermark_id, pending_id, now)
        if window is None:
            # First run with this code: start watching from here.
            store.set(WATERMARK_KEY, str(snowflake_for(now)))
            log("[WebhookCatchup] no watermark yet; starting one now (nothing to replay)")
            return None

        after_id, before_id, clamped = window
        if pending_id is None:
            # plan_window returned a window, so the watermark is set.
            store.set(PENDING_KEY, str(watermark_id))
        since = snowflake_time(after_id)
        log(f"[WebhookCatchup] replaying webhook channels from "
            f"{since:%Y-%m-%d %H:%M:%S} UTC ({(now - since).total_seconds() / 60:.0f} min)")
        if clamped:
            log(f"[WebhookCatchup] WARNING: the gap is longer than "
                f"{MAX_LOOKBACK.days} days; older messages need "
                f"scripts/replay_webhook_window.py (rows would be dated now)")

        _mark_active(store, after_id)
        own_id = getattr(getattr(client, "user", None), "id", None)
        counts = {"channels": 0, "messages": 0, "dispatched": 0, "failed": 0,
                  "unreadable_channels": 0}
        channels = await collect_channels(client, guild_ids, log=log)
        counts["channels"] = len(channels)
        for guild, channel in channels:
            _mark_active(store, after_id)
            try:
                # interactions.py ignores `before` once `after` is set and pages
                # forward to the present (oldest first), so the upper bound is
                # enforced here. Past it the live listener has it covered.
                async for message in channel.history(limit=0, after=after_id):
                    if int(message.id) >= before_id:
                        break
                    if not should_replay(message, own_id):
                        continue
                    bundle = build_message_bundle(message)
                    if not bundle:
                        continue
                    stamp_bundle(bundle, getattr(message, "created_at", None))
                    counts["messages"] += 1
                    if counts["messages"] % 200 == 0:
                        _mark_active(store, after_id)
                    try:
                        counts["dispatched"] += await process_message_bundle(message, bundle)
                    except Exception as e:
                        counts["failed"] += 1
                        log(f"[WebhookCatchup] ! dispatch failed for message {message.id}: {e}")
                    await asyncio.sleep(PER_MESSAGE_YIELD)
            except Exception as e:
                counts["unreadable_channels"] += 1
                log(f"[WebhookCatchup] ! {getattr(guild, 'name', guild)}"
                    f"#{getattr(channel, 'name', channel)}: history unavailable ({e})")

        # Done: the next catch-up starts from the live watermark again.
        store.delete(PENDING_KEY)
        _clear_active(store)
        log(f"[WebhookCatchup] done: {counts['messages']} message(s) with submissions "
            f"across {counts['channels']} channel(s), {counts['dispatched']} embed(s) "
            f"dispatched, {counts['failed']} failed, "
            f"{counts['unreadable_channels']} channel(s) unreadable")
        return counts
