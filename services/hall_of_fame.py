"""Hall of Fame service — reconciliation-based rewrite.

Every cycle, each enabled group's Hall of Fame channel is converged to a
deterministic "desired state" in a single sequential pass:

    [directory] [boss #1] [boss #2] ... [boss #N] [bottom directory]

(bosses alphabetical, raid modes grouped into one message), or — when the
group has ``hof_individual_boss_messages`` disabled — just:

    [directory]

where the directory carries select menus that answer with an ephemeral
per-boss leaderboard.

Reconciliation is positional: tracked messages (``group_personal_best_message``
rows) are sorted by message id (= channel order) and the i-th message is
edited to hold the i-th plan entry.  Adding a boss in the middle therefore
shifts content through existing messages and only ever *appends* new messages
at the bottom, which keeps the channel alphabetical without deleting or
re-posting anything.  Surplus messages are deleted from the bottom up.

Design rules that fix the historical bugs (see GitHub issue #31):
- One pass per group at a time, groups processed strictly sequentially: no
  concurrent workers racing sends against cleanup (the duplicate-message bug).
- Cleanup of untracked bot messages happens at the start of a group's pass,
  before any sends, and only touches messages older than 2 minutes.
- Content hashes are keyed by *message id* in Redis, so a hash can never be
  compared against the wrong message after a re-mapping.
- ``session.expire_all()`` runs before every group pass so freshly-inserted
  PB rows are always visible (the stale-PB bug).
- All Discord writes go through one rate-limited, retrying helper (per-channel
  pacing ~1 write/1.2s plus a global cap), honouring 429 retry_after and
  putting groups that 403 on a cooldown.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import interactions
from interactions import (
    ActionRow,
    BaseComponent,
    ComponentContext,
    Extension,
    StringSelectMenu,
    StringSelectOption,
    UnfurledMediaItem,
    listen,
)
from interactions.api.events import Component, GuildJoin
from interactions.client.errors import BadRequest, Forbidden, HTTPException, NotFound, RateLimited
from interactions.models import (
    ContainerComponent,
    SectionComponent,
    SeparatorComponent,
    TextDisplayComponent,
    ThumbnailComponent,
)

from db.entitlements import resolve_group_entitlements
from db.models import (
    Group,
    GroupConfiguration,
    GroupPersonalBestMessage,
    NpcList,
    PersonalBestEntry,
    Player,
    get_current_partition,
    session,
)
from db.ops import get_formatted_name
from utils.format import NPC_IMG_DIR, convert_from_ms, format_number, get_npc_image_url
from utils.hof import (
    DIRECTORY_BOTTOM_KEY,
    DIRECTORY_KEY,
    RAID_GROUPS,
    SEPULCHRE_CANONICAL,
    SYNC_NOTE_TEXT,
    BossPlanEntry,
    build_boss_plan,
    build_message_plan,
    canonical_display_name,
    npc_name_candidates,
    chunk_select_options,
    fit_directory_lines,
    parse_boss_list,
    parse_select_custom_id,
    select_menu_custom_id,
)
from utils.redis import redis_client
from utils.site_urls import WEBSITE_URL, npc_url

log = logging.getLogger(__name__)

_MEDAL_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}
_FOOTER_TEXT = (
    f"-# Powered by the [DropTracker]({WEBSITE_URL}) • "
    f"[View all Personal Bests]({WEBSITE_URL}/personal-bests)"
)

# The periodic sweep is now a slow self-heal (deleted-message recovery + loot
# refresh); near-real-time PB freshness comes from the Redis refresh queue (see
# _refresh_loop), so the sweep can run less often to reduce edit pressure.
_CYCLE_SLEEP_SECONDS = int(os.getenv("HOF_CYCLE_SECONDS", "600"))
_GROUP_TIMEOUT_SECONDS = 900
# 403 (missing permissions) is usually a persistent misconfiguration, so back
# off exponentially instead of hammering the same channel every 30 minutes.
# The cap stays low (2h) because the failure is user-fixable at any moment —
# Rancour PvM re-invited the bot and still waited out an 8h cooldown before the
# GuildJoin/channel-change resets below existed. A probe every 2h is one cheap
# fetch; a day of silence after the admin already fixed the problem is not.
_FORBIDDEN_BASE_COOLDOWN_SECONDS = 1800
_FORBIDDEN_MAX_COOLDOWN_SECONDS = 2 * 3600
_CHANNEL_SCAN_LIMIT = 200
_ORPHAN_MIN_AGE_SECONDS = 120
_DISCORD_EPOCH_MS = 1420070400000

# The global/template group is always processed and reads global (not
# group-scoped) leaderboards; it is exempt from the premium entitlement gate.
_GLOBAL_GROUP_ID = 2

# Cross-process near-real-time refresh: pb_processor RPUSHes {player_id, npc_id}
# onto this list when a stored PB changes; _refresh_loop drains it and edits
# just the affected boss message(s) within seconds.
_REFRESH_QUEUE_KEY = "hof:refresh:queue"
_REFRESH_BLPOP_TIMEOUT = 5
_REFRESH_DEBOUNCE_SECONDS = 2.0
# Entitlement resolution runs a few subscription queries; cache the result for a
# short window so the per-cycle and per-refresh gates stay cheap.
_ENTITLEMENT_TTL_SECONDS = 120

# Discord Components V2 limits (with head-room).
_MAX_COMPONENT_COUNT = 38
_MAX_TEXT_CHARS = 3950

# v2: v1 hashes were poisoned by silent edit failures (the interactions HTTP
# client returns None after exhausting 429 retries and Message.edit no-ops).
# Bumping the version forces one clean re-verification wave of every message.
_HASH_KEY_TEMPLATE = "hof:msghash:v2:{group_id}:{message_id}"
_HASH_TTL_SECONDS = 14 * 24 * 3600


class ChannelNotPostable(Exception):
    """The HOF bot cannot use a group's configured channel.

    ``fetch_channel`` returns a bare ``BaseChannel`` (no ``send``/``history``)
    when the bot can't fully access the channel — typically because the HOF bot
    (a separate Discord app from the main bot) isn't in the guild or lacks
    View/Send permission. Treated like a 403 so the group backs off instead of
    crashing its pass every cycle."""


# Both moved to utils/discord_write.py so the recap delivery job shares one
# implementation rather than growing a second, subtly different copy. Re-exported
# here because this module's own code (and its tests) refer to RateLimiter.
from utils.discord_write import DiscordWriter, RateLimiter  # noqa: E402,F401


@dataclass
class GroupHOFConfig:
    channel_id: Optional[str] = None
    boss_names: List[str] = field(default_factory=list)
    # MUST match the web config registry's default (False): the settings page
    # renders a missing row as "off", so the bot treating missing as "on" made
    # the UI lie — Rancour PvM configured directory-only, saw the toggle off,
    # and still got 36 per-boss messages. Groups that ran individual boards on
    # the old implicit default were given explicit '1' rows when this flipped.
    individual_messages: bool = False
    pb_entries: int = 5


@dataclass
class CycleStats:
    groups: int = 0
    edited: int = 0
    sent: int = 0
    deleted: int = 0
    skipped: int = 0
    pruned: int = 0
    failures: int = 0

    def summary(self) -> str:
        return (
            f"{self.groups} groups | {self.edited} edited, {self.sent} sent, "
            f"{self.deleted} deleted, {self.pruned} pruned rows, "
            f"{self.skipped} unchanged, {self.failures} failures"
        )


@dataclass
class ChannelScan:
    """Bot-authored messages found in the HOF channel, newest-first scan."""
    by_id: Dict[str, "interactions.Message"] = field(default_factory=dict)
    exhaustive: bool = False  # True when the scan reached the start of the channel


class HallOfFame(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        # Per-channel pacing: Discord's PATCH message bucket is 5 per rolling
        # window per channel, and production logs show sustained 1.2s pacing
        # still tripping it during mass-edit waves, so pace conservatively.
        self._writer = DiscordWriter(
            label="HOF",
            global_max_calls=6, global_period=1.0,
            bucket_max_calls=1, bucket_period=1.5,
        )
        self._forbidden_until: Dict[int, float] = {}
        self._forbidden_strikes: Dict[int, int] = {}
        # group_id -> channel_id the strike happened on, so a channel
        # re-configuration can void the cooldown instead of waiting it out.
        self._forbidden_channel: Dict[int, str] = {}
        self._entitlement_cache: Dict[int, Tuple[float, bool]] = {}
        # group_id -> (monotonic_ts, player_ids). Membership is re-queried once
        # per group per cycle instead of once per boss (was O(bosses) queries).
        self._player_ids_cache: Dict[int, Tuple[float, List[int]]] = {}
        # Serialises the periodic sweep against the near-real-time refresh
        # consumer: both mutate the shared DB session and edit the same
        # channels, so they must never interleave at an await boundary (the
        # single-writer invariant that fixed the duplicate-message races).
        self._work_lock = asyncio.Lock()
        self._loop_task = asyncio.create_task(self._update_loop())
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        log.warning(
            "HOF: reconciliation service started (sweep every %ds, refresh queue live)",
            _CYCLE_SLEEP_SECONDS,
        )

    def drop(self):
        for task in (getattr(self, "_loop_task", None), getattr(self, "_refresh_task", None)):
            try:
                if task is not None:
                    task.cancel()
            except Exception:
                pass
        super().drop()

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    async def _update_loop(self):
        cycle = 0
        while True:
            cycle += 1
            started = time.monotonic()
            try:
                stats = await self._run_cycle(cycle)
                log.warning(
                    "HOF cycle %d done in %.0fs | %s",
                    cycle, time.monotonic() - started, stats.summary(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("HOF cycle %d: uncaught exception (loop continues)", cycle)
            finally:
                # End the read transaction the cycle's trailing queries left
                # autobegun on the shared session — otherwise it sits idle in
                # innodb_trx (holding its snapshot + metadata locks) for the
                # whole inter-cycle sleep. The next read simply begins a fresh
                # transaction.
                async with self._work_lock:
                    self._safe_rollback()
            await asyncio.sleep(_CYCLE_SLEEP_SECONDS)

    async def _run_cycle(self, cycle: int) -> CycleStats:
        stats = CycleStats()
        # Resolve the eligible groups under the lock, from a FRESH snapshot. The
        # long-running session otherwise holds one REPEATABLE READ view and never
        # sees a group that just toggled create_pb_embeds on until the process
        # restarts. Entitlement is resolved here too so no session read happens
        # outside the lock (which would race the refresh consumer).
        async with self._work_lock:
            self._begin_fresh_read()
            group_ids = [
                row.group_id
                for row in session.query(GroupConfiguration.group_id).filter(
                    GroupConfiguration.config_key == "create_pb_embeds",
                    GroupConfiguration.config_value == "1",
                ).all()
            ]
            dev_mode = self._is_in_development()
            eligible: List[int] = []
            for group_id in group_ids:
                if dev_mode and group_id != _GLOBAL_GROUP_ID:
                    continue
                if not self._group_is_entitled(group_id):
                    stats.skipped += 1
                    continue
                if self._cooldown_active(group_id):
                    continue
                eligible.append(group_id)
        if dev_mode:
            # This is trivially left on after testing and then silently freezes
            # every other group's Hall of Fame — make it impossible to miss.
            log.warning(
                "HOF: is_in_development=1 on group %d — ONLY group %d is being "
                "processed this cycle; ALL OTHER GROUPS ARE SKIPPED. Clear that "
                "config row to resume normal operation.",
                _GLOBAL_GROUP_ID, _GLOBAL_GROUP_ID,
            )
        for group_id in eligible:
            try:
                async with self._work_lock:
                    await asyncio.wait_for(
                        self._reconcile_group(group_id, stats),
                        timeout=_GROUP_TIMEOUT_SECONDS,
                    )
                self._clear_forbidden(group_id)
                stats.groups += 1
            except (Forbidden, ChannelNotPostable) as e:
                self._safe_rollback()
                self._note_forbidden(
                    group_id,
                    reason=str(e) if isinstance(e, ChannelNotPostable) else None,
                    channel_id=getattr(e, "channel_id", None),
                )
                stats.failures += 1
            except NotFound as e:
                self._safe_rollback()
                stats.failures += 1
                log.warning("HOF: group %d channel/guild missing: %s", group_id, e)
            except asyncio.TimeoutError:
                self._safe_rollback()
                stats.failures += 1
                log.error("HOF: group %d pass timed out after %ds", group_id, _GROUP_TIMEOUT_SECONDS)
            except Exception:
                self._safe_rollback()
                stats.failures += 1
                log.exception("HOF: group %d pass failed", group_id)
        return stats

    # ------------------------------------------------------------------ #
    # Entitlement gate + 403 backoff
    # ------------------------------------------------------------------ #

    def _group_is_entitled(self, group_id: int) -> bool:
        """True when the group may run a Hall of Fame this cycle.

        The global/template group is always allowed; every other group must
        currently hold the ``hall_of_fame`` premium entitlement (this is what
        gates editing the HOF config on the website, so runtime and paywall
        stay consistent and lapsed groups stop being processed). Cached for a
        short window to keep the per-cycle/per-refresh cost negligible.
        """
        if group_id == _GLOBAL_GROUP_ID:
            return True
        now = time.monotonic()
        cached = self._entitlement_cache.get(group_id)
        if cached and now - cached[0] < _ENTITLEMENT_TTL_SECONDS:
            return cached[1]
        try:
            granted = bool(resolve_group_entitlements(session, group_id).get("hall_of_fame"))
        except Exception:
            self._safe_rollback()
            # Fall back to the last known answer if we have one; otherwise fail
            # closed rather than crash the cycle.
            granted = cached[1] if cached else False
        self._entitlement_cache[group_id] = (now, granted)
        return granted

    def _note_forbidden(self, group_id: int, reason: Optional[str] = None,
                        channel_id=None):
        """Record a 403 / inaccessible channel and back off exponentially
        (30m, 1h, 2h capped). Remembers the offending channel so a channel
        re-configuration voids the cooldown immediately."""
        strikes = self._forbidden_strikes.get(group_id, 0) + 1
        self._forbidden_strikes[group_id] = strikes
        cooldown = min(
            _FORBIDDEN_BASE_COOLDOWN_SECONDS * (2 ** (strikes - 1)),
            _FORBIDDEN_MAX_COOLDOWN_SECONDS,
        )
        self._forbidden_until[group_id] = time.monotonic() + cooldown
        if channel_id is None:
            channel_id = self._configured_channel_id(group_id)
        if channel_id:
            self._forbidden_channel[group_id] = str(channel_id)
        detail = reason or ("bot lacks permission to post/edit in its Hall of "
                            "Fame channel")
        log.warning(
            "HOF: group %d not postable (strike %d) — %s; backing off %.0fmin",
            group_id, strikes, detail, cooldown / 60.0,
        )

    def _clear_forbidden(self, group_id: int):
        """A group pass succeeded (or its blocker went away), so reset its
        403 backoff state."""
        self._forbidden_strikes.pop(group_id, None)
        self._forbidden_until.pop(group_id, None)
        self._forbidden_channel.pop(group_id, None)

    def _configured_channel_id(self, group_id: int) -> Optional[str]:
        try:
            row = session.query(GroupConfiguration).filter(
                GroupConfiguration.group_id == group_id,
                GroupConfiguration.config_key == "channel_id_to_send_pb_embeds",
            ).first()
            return str(row.config_value) if row and row.config_value else None
        except Exception:
            self._safe_rollback()
            return None

    def _cooldown_active(self, group_id: int) -> bool:
        """True while a 403 backoff is in force — unless the group re-configured
        its Hall of Fame channel since the strike, which voids the cooldown (the
        admin plainly acted; waiting out hours of backoff against the OLD
        channel is what stranded Rancour PvM). Called under ``_work_lock``."""
        until = self._forbidden_until.get(group_id)
        if not until or time.monotonic() >= until:
            return False
        struck_channel = self._forbidden_channel.get(group_id)
        if struck_channel:
            current = self._configured_channel_id(group_id)
            if current and current != struck_channel:
                log.warning(
                    "HOF: group %d re-configured its channel (%s -> %s) — "
                    "clearing backoff and retrying this cycle",
                    group_id, struck_channel, current,
                )
                self._clear_forbidden(group_id)
                return False
        return True

    # ------------------------------------------------------------------ #
    # Near-real-time refresh consumer
    # ------------------------------------------------------------------ #

    async def _refresh_loop(self):
        """Drain the cross-process refresh queue and edit just the affected
        boss message(s) so a new PB appears within seconds instead of waiting
        for the next sweep.

        Runs in the same event loop as the sweep and shares ``self._work_lock``
        with it, so the two never interleave — preserving the single-writer
        invariant that keeps the channel from duplicating/desyncing.
        """
        # Don't touch channels until the gateway is connected.
        for _ in range(150):
            if getattr(self.bot, "is_ready", False):
                break
            await asyncio.sleep(2)
        while True:
            try:
                # The blocking drain must stay OUTSIDE the lock (it parks for up
                # to _REFRESH_BLPOP_TIMEOUT and would otherwise stall the sweep).
                raw_items = await self._drain_refresh_queue()
                if not raw_items:
                    continue
                # Resolving touches the shared DB session, so it must hold the
                # same lock as the sweep — the two never use the session at once.
                async with self._work_lock:
                    targets = self._resolve_refresh_targets(raw_items)
                for group_id, display_name in targets:
                    try:
                        async with self._work_lock:
                            await self._refresh_single_boss(group_id, display_name)
                    except Forbidden:
                        self._safe_rollback()
                        self._note_forbidden(group_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._safe_rollback()
                        log.exception(
                            "HOF: refresh failed for group %d boss '%s'", group_id, display_name,
                        )
                # _resolve_refresh_targets (and any refresh that ended in a
                # read) left an open transaction on the shared session; end it
                # so it doesn't idle until the next signal arrives.
                async with self._work_lock:
                    self._safe_rollback()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("HOF: refresh loop iteration failed (loop continues)")
                await asyncio.sleep(1)

    async def _drain_refresh_queue(self) -> List:
        """Block for one refresh signal, then coalesce any others that arrive
        within the debounce window. Returns the raw queue payloads (no DB work
        here — resolution happens under the work lock in the caller)."""
        first = await asyncio.to_thread(
            redis_client.client.blpop, _REFRESH_QUEUE_KEY, _REFRESH_BLPOP_TIMEOUT,
        )
        if not first:
            return []
        raw_items = [first[1]]
        # Coalesce a burst (e.g. several players PBing the same boss at once)
        # so we edit each message once rather than N times.
        await asyncio.sleep(_REFRESH_DEBOUNCE_SECONDS)
        while True:
            more = await asyncio.to_thread(redis_client.client.lpop, _REFRESH_QUEUE_KEY)
            if not more:
                break
            raw_items.append(more)
            if len(raw_items) >= 500:  # safety valve against unbounded drains
                break
        return raw_items

    def _resolve_refresh_targets(self, raw_items: List) -> Set[Tuple[int, str]]:
        """Map raw {player_id, npc_id} signals to the set of boss messages that
        must be re-rendered, applying the same dev/entitlement gates as the
        sweep so lapsed or dev-suppressed groups are never touched here."""
        targets: Set[Tuple[int, str]] = set()
        parsed: List[Tuple[int, int]] = []
        for item in raw_items:
            try:
                if isinstance(item, bytes):
                    item = item.decode("utf-8")
                payload = json.loads(item)
                parsed.append((int(payload["player_id"]), int(payload["npc_id"])))
            except Exception:
                continue
        if not parsed:
            return targets
        try:
            self._begin_fresh_read()
            dev_mode = self._is_in_development()
            # npc_id -> canonical display name (one query per distinct npc).
            display_by_npc: Dict[int, Optional[str]] = {}
            for _, npc_id in parsed:
                if npc_id in display_by_npc:
                    continue
                npc = session.query(NpcList).filter(NpcList.npc_id == npc_id).first()
                display_by_npc[npc_id] = canonical_display_name(npc.npc_name) if npc else None
            for player_id, npc_id in parsed:
                display_name = display_by_npc.get(npc_id)
                if not display_name:
                    continue
                player = session.query(Player).filter(Player.player_id == player_id).first()
                if not player:
                    continue
                for group in (player.groups or []):
                    gid = group.group_id
                    if dev_mode and gid != _GLOBAL_GROUP_ID:
                        continue
                    if not self._group_is_entitled(gid):
                        continue
                    cfg = self._load_group_config(gid)
                    if not cfg.individual_messages or not cfg.boss_names:
                        continue
                    plan_names = {e.display_name for e in build_boss_plan(cfg.boss_names)}
                    # The group's plan key may be a spelling variant of the
                    # NpcList name ('Leviathan' vs 'The Leviathan') — match on
                    # candidates and target the PLAN's key, since that is what
                    # group_personal_best_message.boss_name stores.
                    matched = next(
                        (c for c in npc_name_candidates(display_name) if c in plan_names),
                        None,
                    )
                    if matched:
                        targets.add((gid, matched))
        except Exception:
            self._safe_rollback()
            log.exception("HOF: failed to resolve refresh targets")
        return targets

    async def _refresh_single_boss(self, group_id: int, display_name: str):
        """Re-render one boss message in place (by message id, not positionally)
        and edit it only if its content actually changed."""
        self._begin_fresh_read()
        group = session.query(Group).filter(Group.group_id == group_id).first()
        if not group or not group.guild_id:
            return
        cfg = self._load_group_config(group_id)
        if not cfg.channel_id or not cfg.individual_messages:
            return
        resolved = await self._resolve_entries(group_id, build_boss_plan(cfg.boss_names))
        match = next(((e, npcs) for e, npcs in resolved if e.display_name == display_name), None)
        if match is None:
            return
        row = session.query(GroupPersonalBestMessage).filter(
            GroupPersonalBestMessage.group_id == group_id,
            GroupPersonalBestMessage.boss_name == display_name,
            GroupPersonalBestMessage.channel_id == str(cfg.channel_id),
        ).order_by(GroupPersonalBestMessage.message_id.asc()).first()
        if row is None:
            # Not materialised yet — the periodic sweep will create it.
            return
        channel = await self.bot.fetch_channel(int(cfg.channel_id))
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        directory_url = self._directory_jump_url(group)
        entry, npcs = match
        components = self._render_boss_entry(group, entry, npcs, directory_url, cfg)
        if not components:
            return
        new_hash = self._components_hash(components)
        if self._get_stored_hash(group_id, row.message_id) == new_hash:
            return
        try:
            message = await self._get_channel_message(channel, row.message_id, scan=None)
        except Exception as e:
            log.warning("HOF: refresh fetch failed for group %d boss '%s': %s",
                        group_id, display_name, e)
            return
        if message is None:
            return  # deleted — the sweep will recreate it in the right slot
        await self._discord_write(
            str(channel.id), lambda: message.edit(components=components), expect_result=True,
        )
        row.date_updated = datetime.datetime.now()
        session.commit()
        self._store_hash(group_id, row.message_id, new_hash)
        log.info("HOF: refreshed group %d boss '%s' from PB signal", group_id, display_name)

    def _directory_jump_url(self, group: Group) -> Optional[str]:
        """Jump URL of the group's top directory message, if it exists."""
        row = session.query(GroupPersonalBestMessage).filter(
            GroupPersonalBestMessage.group_id == group.group_id,
            GroupPersonalBestMessage.boss_name == DIRECTORY_KEY,
        ).order_by(GroupPersonalBestMessage.message_id.asc()).first()
        return self._jump_url(group, row) if row is not None else None

    def _safe_rollback(self):
        """Discard any uncommitted session state so an aborted group pass can
        never leak pending deletes/updates into the next group's commit."""
        try:
            session.rollback()
        except Exception:
            pass

    def _begin_fresh_read(self):
        """Reset the shared session to a fresh DB snapshot before a read pass.

        The module-level session holds a single long-lived REPEATABLE READ
        transaction, so an externally-committed change (e.g. a group that just
        toggled create_pb_embeds on via the website) is invisible to plain
        ``expire_all()`` until the process restarts. Rolling back first ends the
        stale transaction; the next query autobegins one with a current
        snapshot. Must be called while holding ``_work_lock``."""
        try:
            session.rollback()
        except Exception:
            pass
        session.expire_all()

    def _is_in_development(self) -> bool:
        cfg = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == 2,
            GroupConfiguration.config_key == "is_in_development",
        ).first()
        return bool(cfg and cfg.config_value == "1")

    # ------------------------------------------------------------------ #
    # Group reconciliation
    # ------------------------------------------------------------------ #

    def _load_group_config(self, group_id: int) -> GroupHOFConfig:
        cfg = GroupHOFConfig()
        rows = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group_id,
            GroupConfiguration.config_key.in_([
                "channel_id_to_send_pb_embeds",
                "personal_best_embed_boss_list",
                "hof_individual_boss_messages",
                "number_of_pbs_to_display",
            ]),
        ).all()
        for row in rows:
            if row.config_key == "channel_id_to_send_pb_embeds":
                cfg.channel_id = row.config_value or None
            elif row.config_key == "personal_best_embed_boss_list":
                cfg.boss_names = parse_boss_list(row.config_value, row.long_value)
            elif row.config_key == "hof_individual_boss_messages":
                cfg.individual_messages = str(row.config_value).strip().lower() not in ("0", "false", "no", "off")
            elif row.config_key == "number_of_pbs_to_display":
                # Legacy sentinel: nearly every group carries '0' (cloned from
                # the template group), which has always meant "unset — use the
                # default". Clamping it to 1 collapses every individual boss
                # message to a single PB per bracket.
                try:
                    value = int(row.config_value)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    cfg.pb_entries = min(10, value)
        return cfg

    async def _resolve_entries(
        self, group_id: int, entries: List[BossPlanEntry]
    ) -> List[Tuple[BossPlanEntry, List[NpcList]]]:
        """Attach NpcList rows to each plan entry, dropping unresolvable bosses.

        Configured names are user-entered and often miss NpcList's exact
        spelling ('Leviathan' vs 'The Leviathan'), so each variant tries the
        deterministic candidate spellings before being declared missing.
        """
        resolved: List[Tuple[BossPlanEntry, List[NpcList]]] = []
        for entry in entries:
            npcs: List[NpcList] = []
            for name in entry.variant_names:
                npc = None
                for candidate in npc_name_candidates(name):
                    npc = session.query(NpcList).filter(NpcList.npc_name == candidate).first()
                    if npc:
                        break
                if npc:
                    npcs.append(npc)
                    await self._ensure_npc_image(npc)
                else:
                    log.warning("HOF: group %d boss '%s' not in NpcList", group_id, name)
            if npcs:
                resolved.append((entry, npcs))
        return resolved

    async def _ensure_npc_image(self, npc: NpcList) -> None:
        """Best-effort on-demand fetch of a boss's HOF thumbnail if it's not cached
        on disk yet (e.g. a newly-minted NpcList row). Failures are non-fatal —
        `_get_npc_img_url` falls back to a same-raid image if one is missing."""
        if os.path.exists(f"{NPC_IMG_DIR}/{npc.npc_id}.png"):
            return
        try:
            await get_npc_image_url(npc.npc_name, npc.npc_id)
        except Exception:
            log.warning("HOF: failed to fetch image for npc '%s' (%d)", npc.npc_name, npc.npc_id, exc_info=True)

    async def _reconcile_group(self, group_id: int, stats: CycleStats):
        self._begin_fresh_read()
        group = session.query(Group).filter(Group.group_id == group_id).first()
        if not group or not group.guild_id:
            return
        cfg = self._load_group_config(group_id)
        if not cfg.channel_id:
            log.warning("HOF: group %d has no channel_id_to_send_pb_embeds", group_id)
            return

        channel = await self.bot.fetch_channel(int(cfg.channel_id))
        if channel is None:
            log.warning("HOF: group %d channel %s not found", group_id, cfg.channel_id)
            return
        if not hasattr(channel, "send") or not hasattr(channel, "history"):
            # Degraded BaseChannel: the HOF bot can't access this channel.
            exc = ChannelNotPostable(
                f"group {group_id} channel {cfg.channel_id} is not accessible "
                f"to the HOF bot (not in guild or missing permissions)"
            )
            exc.channel_id = cfg.channel_id
            raise exc

        if not cfg.boss_names:
            # Never treat an empty (possibly accidentally wiped) boss list as a
            # request to tear the channel down — leave existing messages alone.
            log.warning("HOF: group %d has no bosses configured, skipping", group_id)
            return
        resolved = await self._resolve_entries(group_id, build_boss_plan(cfg.boss_names))
        if not resolved:
            log.error("HOF: group %d — none of %d configured bosses resolved, skipping",
                      group_id, len(cfg.boss_names))
            return
        entry_by_key = {entry.display_name: (entry, npcs) for entry, npcs in resolved}
        plan = build_message_plan([entry.display_name for entry, _ in resolved], cfg.individual_messages)

        scan = await self._scan_channel(channel)
        slots = await self._load_verified_slots(group, channel, scan, stats)
        await self._delete_orphans(group, channel, scan, {str(r.message_id) for r in slots}, stats)

        # Slot 0 (the directory) must exist first so boss messages can link to it.
        if not slots:
            slots.append(await self._send_new_message(
                group, channel, DIRECTORY_KEY, self._render_placeholder(), stats,
            ))
        directory_url = self._jump_url(group, slots[0])

        # Boss messages: edit in place (positional), append new ones at the bottom.
        for index, key in enumerate(plan):
            if key in (DIRECTORY_KEY, DIRECTORY_BOTTOM_KEY):
                continue
            entry, npcs = entry_by_key[key]
            try:
                components = self._render_boss_entry(group, entry, npcs, directory_url, cfg)
            except Exception:
                stats.failures += 1
                log.exception("HOF: group %d failed to render '%s'", group_id, key)
                continue
            if components is None:
                stats.failures += 1
                continue
            await self._apply_slot(group, channel, scan, slots, index, key, components, stats)

        # Remove surplus messages (e.g. bosses un-configured, or a switch to
        # directory-only mode) from the bottom of the channel.
        while len(slots) > len(plan):
            row = slots.pop()
            await self._delete_slot(group, channel, scan, row, stats)

        # Directories last, once every boss message id is final.  The sync note
        # rides on whichever directory is physically last in the channel: the
        # bottom one in individual-boss mode, the only one otherwise.
        row_by_key = {row.boss_name: row for row in slots}
        last_key = plan[-1]
        if DIRECTORY_BOTTOM_KEY in plan:
            components = self._render_directory(
                group, resolved, row_by_key, cfg, top_directory_url=directory_url,
                include_sync_note=last_key == DIRECTORY_BOTTOM_KEY,
            )
            await self._apply_slot(
                group, channel, scan, slots,
                plan.index(DIRECTORY_BOTTOM_KEY), DIRECTORY_BOTTOM_KEY, components, stats,
            )
        components = self._render_directory(
            group, resolved, row_by_key, cfg, top_directory_url=None,
            include_sync_note=last_key == DIRECTORY_KEY,
        )
        await self._apply_slot(group, channel, scan, slots, 0, DIRECTORY_KEY, components, stats)

    # ------------------------------------------------------------------ #
    # Channel state: scan, verify, cleanup
    # ------------------------------------------------------------------ #

    async def _scan_channel(self, channel) -> Optional[ChannelScan]:
        """One history read gives us existence checks, Message objects for edits,
        and the orphan list — without per-message fetches."""
        try:
            scan = ChannelScan()
            bot_user_id = str(self.bot.user.id)
            count = 0
            async for message in channel.history(limit=_CHANNEL_SCAN_LIMIT):
                count += 1
                if str(message.author.id) == bot_user_id:
                    scan.by_id[str(message.id)] = message
            scan.exhaustive = count < _CHANNEL_SCAN_LIMIT
            return scan
        except (Forbidden, NotFound):
            raise
        except Exception as e:
            log.warning("HOF: channel scan failed for %s: %s", channel.id, e)
            return None

    async def _load_verified_slots(
        self, group: Group, channel, scan: Optional[ChannelScan], stats: CycleStats,
    ) -> List[GroupPersonalBestMessage]:
        rows = session.query(GroupPersonalBestMessage).filter(
            GroupPersonalBestMessage.group_id == group.group_id,
        ).all()
        valid = [r for r in rows if r.message_id and str(r.message_id).isdigit()]
        for bad in (r for r in rows if r not in valid):
            session.delete(bad)
            stats.pruned += 1
        valid.sort(key=lambda r: int(r.message_id))

        # Two rows pointing at the same Discord message would make positional
        # mapping edit the same message under two plan keys, churning forever.
        seen_ids: set[str] = set()
        deduped: List[GroupPersonalBestMessage] = []
        for row in valid:
            if str(row.message_id) in seen_ids:
                session.delete(row)
                stats.pruned += 1
                continue
            seen_ids.add(str(row.message_id))
            deduped.append(row)
        valid = deduped

        slots: List[GroupPersonalBestMessage] = []
        for row in valid:
            if str(row.channel_id) != str(channel.id):
                # Channel was re-configured: the old message is unreachable now.
                session.delete(row)
                stats.pruned += 1
                continue
            if await self._message_exists(channel, row, scan):
                slots.append(row)
            else:
                session.delete(row)
                stats.pruned += 1
        session.commit()
        return slots

    async def _message_exists(
        self, channel, row: GroupPersonalBestMessage, scan: Optional[ChannelScan],
    ) -> bool:
        if scan is not None:
            if str(row.message_id) in scan.by_id:
                return True
            if scan.exhaustive:
                return False
        try:
            message = await channel.fetch_message(int(row.message_id))
            return message is not None
        except NotFound:
            return False
        except Forbidden:
            raise
        except Exception as e:
            log.warning("HOF: existence check for message %s failed, keeping row: %s", row.message_id, e)
            return True

    async def _delete_orphans(
        self, group: Group, channel, scan: Optional[ChannelScan],
        tracked_ids: set, stats: CycleStats,
    ):
        """Delete bot messages in the HOF channel that we do not track (duplicates
        left behind by crashes/old versions).  Runs before any sends this pass and
        skips very recent messages, so it can never race our own writes."""
        if scan is None:
            return
        now_ms = time.time() * 1000
        for message_id, message in scan.by_id.items():
            if message_id in tracked_ids:
                continue
            created_ms = (int(message_id) >> 22) + _DISCORD_EPOCH_MS
            if now_ms - created_ms < _ORPHAN_MIN_AGE_SECONDS * 1000:
                continue
            try:
                await self._discord_write(str(channel.id), message.delete)
                stats.deleted += 1
            except NotFound:
                pass
            except Forbidden:
                raise
            except Exception as e:
                log.warning("HOF: group %d failed to delete orphan %s: %s", group.group_id, message_id, e)

    # ------------------------------------------------------------------ #
    # Applying the plan to Discord
    # ------------------------------------------------------------------ #

    async def _apply_slot(
        self, group: Group, channel, scan: Optional[ChannelScan],
        slots: List[GroupPersonalBestMessage], index: int, key: str,
        components: List[BaseComponent], stats: CycleStats,
    ):
        new_hash = self._components_hash(components)
        if index < len(slots):
            row = slots[index]
            if row.boss_name != key:
                row.boss_name = key
                session.commit()
            if self._get_stored_hash(group.group_id, row.message_id) == new_hash:
                stats.skipped += 1
                return
            try:
                message = await self._get_channel_message(channel, row.message_id, scan)
            except Exception as e:
                # Transient fetch failure: leave this slot for the next cycle
                # rather than mistaking it for a deleted message (a delete +
                # resend here would create a real duplicate in the channel).
                stats.failures += 1
                log.warning("HOF: group %d slot %d fetch failed, skipping this cycle: %s",
                            group.group_id, index, e)
                return
            if message is not None:
                try:
                    await self._discord_write(
                        str(channel.id),
                        lambda: message.edit(components=components),
                        expect_result=True,
                    )
                    row.date_updated = datetime.datetime.now()
                    session.commit()
                    self._store_hash(group.group_id, row.message_id, new_hash)
                    stats.edited += 1
                    return
                except NotFound:
                    message = None
            # The message disappeared mid-pass: replace it.  The new message is
            # appended at the bottom (position self-heals next cycle).
            session.delete(row)
            session.commit()
            stats.pruned += 1
            slots[index] = await self._send_new_message(group, channel, key, components, stats)
        else:
            slots.append(await self._send_new_message(group, channel, key, components, stats))

    async def _send_new_message(
        self, group: Group, channel, key: str,
        components: List[BaseComponent], stats: CycleStats,
    ) -> GroupPersonalBestMessage:
        message = await self._discord_write(
            str(channel.id),
            lambda: channel.send(components=components),
            expect_result=True,
        )
        row = GroupPersonalBestMessage(
            group_id=group.group_id,
            message_id=str(message.id),
            channel_id=str(channel.id),
            boss_name=key,
        )
        session.add(row)
        session.commit()
        self._store_hash(group.group_id, str(message.id), self._components_hash(components))
        stats.sent += 1
        return row

    async def _delete_slot(
        self, group: Group, channel, scan: Optional[ChannelScan],
        row: GroupPersonalBestMessage, stats: CycleStats,
    ):
        try:
            message = await self._get_channel_message(channel, row.message_id, scan)
        except Exception as e:
            # Keep the row so the deletion is retried next cycle.
            stats.failures += 1
            log.warning("HOF: group %d surplus message %s fetch failed, retrying next cycle: %s",
                        group.group_id, row.message_id, e)
            return
        if message is not None:
            try:
                await self._discord_write(str(channel.id), message.delete)
                stats.deleted += 1
            except NotFound:
                pass
        session.delete(row)
        session.commit()

    async def _get_channel_message(
        self, channel, message_id, scan: Optional[ChannelScan],
    ) -> Optional["interactions.Message"]:
        """Return the Message, or None only when it provably no longer exists.

        Transient errors propagate: callers must not confuse "couldn't fetch
        right now" with "deleted", or they will resend and duplicate.
        """
        if scan is not None and str(message_id) in scan.by_id:
            return scan.by_id[str(message_id)]
        try:
            return await channel.fetch_message(int(message_id))
        except NotFound:
            return None

    async def _discord_write(self, channel_id: str, factory, expect_result: bool = False):
        """One paced, retrying Discord write — see :mod:`utils.discord_write`.

        ``expect_result`` MUST be True for sends and edits: treating the
        library's silent None-on-429 as success is what poisoned the hash cache
        and froze stale boss messages in place (the duplicate-boss symptom of
        issue #31).
        """
        return await self._writer.write(channel_id, factory, expect_result=expect_result)

    def _jump_url(self, group: Group, row: GroupPersonalBestMessage) -> str:
        return f"https://discord.com/channels/{group.guild_id}/{row.channel_id}/{row.message_id}"

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render_placeholder(self) -> List[BaseComponent]:
        return [ContainerComponent(
            SeparatorComponent(divider=True),
            TextDisplayComponent(content="## 🏆 Hall of Fame Directory\n-# Setting up… this message updates automatically."),
            SeparatorComponent(divider=True),
        )]

    def _render_directory(
        self, group: Group,
        resolved: List[Tuple[BossPlanEntry, List[NpcList]]],
        row_by_key: Dict[str, GroupPersonalBestMessage],
        cfg: GroupHOFConfig,
        top_directory_url: Optional[str],
        include_sync_note: bool = False,
    ) -> List[BaseComponent]:
        display_names = [entry.display_name for entry, _ in resolved]
        linked_lines: List[str] = []
        plain_lines: List[str] = []
        for name in display_names:
            plain_lines.append(f"- {name}")
            row = row_by_key.get(name) if cfg.individual_messages else None
            if row is not None:
                linked_lines.append(f"- [{name}]({self._jump_url(group, row)})")
            else:
                linked_lines.append(f"- {name}")

        # _render_directory has no shrink-and-retry loop, so the boss list must
        # give back exactly the room the sync note takes or a long directory
        # would silently blow Discord's 4000-char message cap.
        limit = 3300 - (len(SYNC_NOTE_TEXT) + 1 if include_sync_note else 0)
        lines = fit_directory_lines(linked_lines, plain_lines, limit=limit)
        if not lines:
            lines = ["-# No Hall of Fame bosses are configured yet."]

        menu_rows: List[BaseComponent] = []
        for chunk_index, chunk in enumerate(chunk_select_options(display_names)[:8]):
            if len(display_names) <= 25:
                placeholder = "View a boss leaderboard…"
            else:
                placeholder = f"{chunk[0]} → {chunk[-1]}"[:150]
            menu_rows.append(ActionRow(StringSelectMenu(
                *[StringSelectOption(label=name[:100], value=name[:100]) for name in chunk],
                placeholder=placeholder,
                custom_id=select_menu_custom_id(group.group_id, chunk_index),
            )))

        body = "## 🏆 Hall of Fame Directory\n" + "\n".join(lines)
        if menu_rows:
            body += "\n\n-# Pick a boss below to view its leaderboard."
        if top_directory_url:
            body += f"\n-# 📋 [Jump to the top of the Hall of Fame]({top_directory_url})"

        note_components: List[BaseComponent] = []
        if include_sync_note:
            note_components = [
                SeparatorComponent(divider=True),
                TextDisplayComponent(content=SYNC_NOTE_TEXT),
            ]

        return [ContainerComponent(
            SeparatorComponent(divider=True),
            TextDisplayComponent(content=body),
            SeparatorComponent(divider=True),
            *menu_rows,
            SeparatorComponent(divider=True),
            TextDisplayComponent(content=_FOOTER_TEXT),
            *note_components,
        )]

    def _render_boss_entry(
        self, group: Group, entry: BossPlanEntry, npcs: List[NpcList],
        directory_url: Optional[str], cfg: GroupHOFConfig,
    ) -> Optional[List[BaseComponent]]:
        """Render a boss (or grouped raid) message, shrinking until it fits
        Discord's component/text limits.  Returns None if it cannot fit."""
        if entry.grouped:
            attempts = [3, 2, 1]
            render = lambda n: self._render_grouped_boss(group, entry.display_name, npcs, directory_url, n)
        else:
            attempts = sorted({cfg.pb_entries, 5, 3, 2, 1}, reverse=True)
            attempts = [n for n in attempts if n <= cfg.pb_entries] or [1]
            render = lambda n: self._render_individual_boss(group, npcs[0], directory_url, n)

        label = f"group={group.group_id} boss={entry.display_name}"
        for max_entries in attempts:
            components = render(max_entries)
            if self._within_limits(components):
                return components
            log.warning("HOF: %s too large at %d entries/bracket, shrinking", label, max_entries)
        log.error("HOF: %s exceeds Discord limits even at minimum size", label)
        return None

    def _render_individual_boss(
        self, group: Group, npc: NpcList, directory_url: Optional[str], max_entries: int,
    ) -> List[BaseComponent]:
        pb_components, summary_content = self._build_pb_body(
            group.group_id, npc, max_entries=max_entries, include_loot=True,
        )
        return [ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[TextDisplayComponent(
                    content=f"## {self._get_linked_name(npc)} 🏆\n{summary_content}",
                )],
                accessory=ThumbnailComponent(
                    media=UnfurledMediaItem(url=self._get_npc_img_url(npc)),
                ),
            ),
            SeparatorComponent(divider=True),
            *pb_components,
            SeparatorComponent(divider=True),
            *self._trailing_components(directory_url),
        )]

    def _render_grouped_boss(
        self, group: Group, canonical_name: str, npcs: List[NpcList],
        directory_url: Optional[str], max_entries: int,
    ) -> List[BaseComponent]:
        mode_order = {
            "Entry": 0,
            "Normal": 1,
            "Hard Mode": 2,
            "Challenge Mode": 2,
            "Expert": 2,
            "Nightmare": 0,
            "Phosani's Nightmare": 1,
            "Crystalline": 0,
            "Corrupted": 1,
        }
        mode_npcs = [(self._get_variant_mode_name(canonical_name, npc.npc_name), npc) for npc in npcs]
        # Alias NPC rows (e.g. "Nightmare" and "The Nightmare") map to the same
        # mode — keep only the first so a mode section never renders twice.
        seen_modes: set[str] = set()
        mode_npcs = [
            (mode, npc) for mode, npc in mode_npcs
            if not (mode in seen_modes or seen_modes.add(mode))
        ]
        mode_npcs.sort(key=lambda item: (mode_order.get(item[0], 50), item[0].casefold()))

        grouped_components: List[BaseComponent] = []
        for mode_name, mode_npc in mode_npcs:
            # Loot leaderboards are omitted per-mode to stay under the 4000-char cap.
            pb_components, summary_content = self._build_pb_body(
                group.group_id, mode_npc, max_entries=max_entries, include_loot=False,
            )
            grouped_components.append(TextDisplayComponent(content=f"### {mode_name}\n{summary_content}"))
            grouped_components.extend(pb_components)
            grouped_components.append(SeparatorComponent(divider=True))

        return [ContainerComponent(
            SeparatorComponent(divider=True),
            SectionComponent(
                components=[TextDisplayComponent(content=f"## {canonical_name} 🏆")],
                accessory=ThumbnailComponent(
                    media=UnfurledMediaItem(url=self._get_npc_img_url(self._group_thumbnail_npc(canonical_name, npcs))),
                ),
            ),
            SeparatorComponent(divider=True),
            *grouped_components,
            *self._trailing_components(directory_url),
        )]

    def _group_thumbnail_npc(self, canonical_name: str, npcs: List[NpcList]) -> NpcList:
        """Prefer the base/normal-mode NPC's artwork for a raid group's thumbnail
        (e.g. plain 'Theatre of Blood' over 'Theatre of Blood: Hard Mode') —
        it's the mode most groups configure first and the one most likely to
        already have a cached image. `_get_npc_img_url` still falls back across
        modes if this particular one's file is missing."""
        for variant_name in RAID_GROUPS.get(canonical_name, []):
            for npc in npcs:
                if npc.npc_name == variant_name:
                    return npc
        return npcs[0]

    def _trailing_components(self, directory_url: Optional[str]) -> List[BaseComponent]:
        trailing: List[BaseComponent] = [TextDisplayComponent(content=_FOOTER_TEXT)]
        if directory_url:
            trailing.append(SeparatorComponent(divider=True))
            trailing.append(TextDisplayComponent(content=f"-# 📋 [Back to Directory]({directory_url})"))
        return trailing

    def _build_pb_body(
        self, group_id: int, npc: NpcList, max_entries: int, include_loot: bool,
    ) -> Tuple[List[BaseComponent], str]:
        """Build the leaderboard components + the overview summary for one NPC."""
        pbs = self._get_pbs(group_id, npc.npc_name)
        components: List[BaseComponent] = []

        total_pbs = sum(len(entries) for entries in pbs.values())
        fastest: Optional[PersonalBestEntry] = None
        fastest_team_size = None
        for team_size, entries in pbs.items():
            for pb in entries:
                if fastest is None or pb.personal_best < fastest.personal_best:
                    fastest = pb
                    fastest_team_size = team_size
        fastest_kill_part = ""
        if fastest is not None:
            player = session.query(Player).filter(Player.player_id == fastest.player_id).first()
            fastest_kill_part = (
                f"-# • Fastest kill: `{convert_from_ms(fastest.personal_best)}` "
                f"({self._get_team_size_string(fastest_team_size)})\n"
                f"-# ↳ by {self._player_display(player, group_id)}"
            )

        most_loot_part = ""
        total_loot_part = ""
        month_looters: List[Tuple[Optional[Player], float]] = []
        if include_loot:
            try:
                partition = get_current_partition()
                if group_id != 2:
                    month_key = f"leaderboard:group:{group_id}:npc:{npc.npc_id}:{partition}"
                    all_key = f"leaderboard:group:{group_id}:npc:{npc.npc_id}"
                else:
                    month_key = f"leaderboard:npc:{npc.npc_id}:{partition}"
                    all_key = f"leaderboard:npc:{npc.npc_id}"
                for player_id, score in redis_client.client.zrevrange(month_key, 0, 4, withscores=True):
                    player = session.query(Player).filter(Player.player_id == int(player_id)).first()
                    month_looters.append((player, score))
                if month_looters:
                    top_player, top_score = month_looters[0]
                    most_loot_part = (
                        f"\n-# • Most Loot: `{format_number(top_score)}` gp (this month)\n"
                        f"-# ↳ by {self._player_display(top_player, group_id)}"
                    )
                    total_loot = redis_client.zsum(all_key)
                    if total_loot:
                        total_loot_part = f"-# • Total loot tracked: `{format_number(total_loot)}` gp\n"
            except Exception as e:
                log.warning("HOF: loot lookup failed for group %d npc %d: %s", group_id, npc.npc_id, e)

        summary_content = (
            "📊 **__Overview__**\n"
            f"-# • Total PBs tracked: `{total_pbs}`\n"
            f"{total_loot_part}"
            f"{fastest_kill_part}"
            f"{most_loot_part}"
        )

        if month_looters:
            loot_lines = ""
            for i, (player, score) in enumerate(month_looters):
                rank_prefix = _MEDAL_EMOJIS.get(i + 1, f"{i + 1}.")
                loot_lines += f"-# {rank_prefix} {self._player_display(player, group_id)} - `{format_number(score)}` gp\n"
            components.append(TextDisplayComponent(content=(
                "💰 **__Loot Leaderboard__**\n"
                "-# Top 5 players (this month):\n"
                f"{loot_lines}"
            )))
            components.append(SeparatorComponent(divider=True))

        components.append(TextDisplayComponent(content=":hourglass: **__Personal Best Leaderboards__**\n"))

        # Each team-size bracket is one TextDisplayComponent (header + entries
        # merged) to keep the component count low: two components per bracket
        # across many brackets/raid modes quickly exceeds Discord's 40-component cap.
        for team_size in sorted(pbs.keys(), key=self._team_size_sort_key):
            team_size_string = self._get_team_size_string(team_size)
            pb_text = f"-# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n-# **{team_size_string}**\n"
            for i, pb in enumerate(pbs[team_size][:max_entries]):
                rank_prefix = _MEDAL_EMOJIS.get(i + 1, f"{i + 1}.")
                player = getattr(pb, "player", None)
                pb_text += (
                    f"-# {rank_prefix} `{convert_from_ms(pb.personal_best)}` - "
                    f"{self._player_display(player, group_id)}\n"
                )
            components.append(TextDisplayComponent(content=pb_text))
        return components, summary_content

    def _group_player_ids(self, group_id: int) -> List[int]:
        """Member player_ids for a group, cached briefly so a full render pass
        (many bosses back-to-back) doesn't re-run the membership query per boss."""
        cached = self._player_ids_cache.get(group_id)
        now = time.monotonic()
        if cached and now - cached[0] < _ENTITLEMENT_TTL_SECONDS:
            return cached[1]
        group = session.query(Group).filter(Group.group_id == group_id).first()
        player_ids = list({p.player_id for p in group.get_players()}) if group else []
        self._player_ids_cache[group_id] = (now, player_ids)
        return player_ids

    def _get_pbs(self, group_id: int, npc_name: str) -> Dict[object, List[PersonalBestEntry]]:
        """Personal bests for a group + npc name, bucketed by team size and
        sorted fastest-first within each bucket."""
        npc_ids = [row[0] for row in session.query(NpcList.npc_id).filter(NpcList.npc_name == npc_name).all()]
        if not npc_ids:
            return {}
        player_ids = self._group_player_ids(group_id)
        if not player_ids:
            return {}
        pbs = session.query(PersonalBestEntry).filter(
            PersonalBestEntry.player_id.in_(player_ids),
            PersonalBestEntry.npc_id.in_(npc_ids),
        ).all()

        buckets: Dict[object, List[PersonalBestEntry]] = {}
        for pb in pbs:
            buckets.setdefault(pb.team_size, []).append(pb)
        # Cap at the 5 smallest team sizes so one boss can't flood the message.
        if len(buckets) > 5:
            keep = sorted(buckets.keys(), key=self._team_size_sort_key)[:5]
            buckets = {k: buckets[k] for k in keep}
        for entries in buckets.values():
            entries.sort(key=lambda pb: pb.personal_best)
        return buckets

    def _team_size_sort_key(self, team_size) -> Tuple[int, str]:
        value = str(team_size).strip()
        if value.casefold() in ("solo", "1"):
            return (1, "")
        if value.casefold() == "duo":
            return (2, "")
        if value.casefold() == "trio":
            return (3, "")
        try:
            return (int(value.rstrip("+")), "")
        except ValueError:
            return (99, value.casefold())

    def _get_team_size_string(self, team_size) -> str:
        match team_size:
            case 1 | "1" | "Solo":
                return "Solo"
            case 2 | "2" | "Duo":
                return "Duo"
            case 3 | "3" | "Trio":
                return "Trio"
            case _:
                return f"{team_size} players"

    def _player_display(self, player: Optional[Player], group_id: int) -> str:
        if player is None:
            return "Unknown"
        try:
            return get_formatted_name(player.player_name, group_id, session)
        except Exception:
            return player.player_name or "Unknown"

    def _get_variant_mode_name(self, canonical_name: str, npc_name: str) -> str:
        if canonical_name == "Chambers of Xeric":
            return "Challenge Mode" if ("Challenge" in npc_name or "CM" in npc_name) else "Normal"
        if canonical_name == "Theatre of Blood":
            if "Entry Mode" in npc_name:
                return "Entry"
            return "Hard Mode" if "Hard Mode" in npc_name else "Normal"
        if canonical_name == "Tombs of Amascut":
            if "Entry Mode" in npc_name:
                return "Entry"
            return "Expert" if "Expert" in npc_name else "Normal"
        if canonical_name == "Nightmare of Ashihama":
            return "Phosani's Nightmare" if "Phosani" in npc_name else "Nightmare"
        if canonical_name == "The Gauntlet":
            return "Corrupted" if "Corrupted" in npc_name else "Crystalline"
        if canonical_name == SEPULCHRE_CANONICAL:
            floor_match = re.search(r"Floor\s+(\d+)", npc_name)
            if floor_match:
                return f"Floor {floor_match.group(1)}"
        return npc_name

    def _get_linked_name(self, npc: NpcList) -> str:
        npc_name = npc.npc_name
        if "Theatre" in npc.npc_name:
            if "Hard Mode" in npc.npc_name:
                npc_name = "HM ToB"
            elif "Entry Mode" in npc.npc_name:
                npc_name = "EM ToB"
            else:
                npc_name = "ToB"
        if "Chambers" in npc.npc_name:
            npc_name = "CM CoX" if ("Challenge" in npc.npc_name or "CM" in npc.npc_name) else "CoX"
        if "Tombs" in npc.npc_name:
            if "Expert" in npc.npc_name:
                npc_name = "Expert ToA"
            elif "Entry Mode" in npc.npc_name:
                npc_name = "Entry ToA"
            else:
                npc_name = "ToA"
        if "Nightmare" in npc.npc_name:
            npc_name = "Phosani's" if "Phosani" in npc.npc_name else "NM"
        return f"[{npc_name}]({self._get_npc_url(npc)})"

    def _get_npc_img_url(self, npc: NpcList) -> str:
        if os.path.exists(f"{NPC_IMG_DIR}/{npc.npc_id}.png"):
            return f"https://www.droptracker.io/img/npcdb/{npc.npc_id}.png"
        fallback = self._raid_fallback_npc(npc.npc_name, exclude_npc_id=npc.npc_id)
        if fallback:
            return f"https://www.droptracker.io/img/npcdb/{fallback.npc_id}.png"
        return f"https://www.droptracker.io/img/npcdb/{npc.npc_id}.png"

    def _raid_fallback_npc(self, npc_name: str, exclude_npc_id: Optional[int] = None) -> Optional[NpcList]:
        """If `npc_name` is a raid-mode variant and its own artwork is missing,
        find another mode of the same raid (base/normal mode first) that already
        has an image on disk, so the thumbnail isn't a broken image."""
        canonical = canonical_display_name(npc_name)
        variants = RAID_GROUPS.get(canonical)
        if not variants:
            return None
        for variant_name in variants:
            for candidate in npc_name_candidates(variant_name):
                candidate_npc = session.query(NpcList).filter(NpcList.npc_name == candidate).first()
                if not candidate_npc or candidate_npc.npc_id == exclude_npc_id:
                    continue
                if os.path.exists(f"{NPC_IMG_DIR}/{candidate_npc.npc_id}.png"):
                    return candidate_npc
        return None

    def _get_npc_url(self, npc: NpcList) -> str:
        return npc_url(npc.npc_id)

    # ------------------------------------------------------------------ #
    # Limits + hashing
    # ------------------------------------------------------------------ #

    def _within_limits(self, components: List[BaseComponent]) -> bool:
        try:
            dicts = [c.to_dict() for c in components]
        except Exception:
            log.exception("HOF: failed to serialize components for limit check")
            return False
        return (
            self._count_components(dicts) <= _MAX_COMPONENT_COUNT
            and self._total_text(dicts) <= _MAX_TEXT_CHARS
        )

    def _count_components(self, obj) -> int:
        if isinstance(obj, list):
            return sum(self._count_components(item) for item in obj)
        if isinstance(obj, dict):
            count = 1 if "type" in obj else 0
            for child_key in ("components", "accessory", "options"):
                child = obj.get(child_key)
                if child_key != "options" and child is not None:
                    count += self._count_components(child)
            return count
        return 0

    def _total_text(self, obj) -> int:
        if isinstance(obj, list):
            return sum(self._total_text(item) for item in obj)
        if isinstance(obj, dict):
            total = 0
            content = obj.get("content")
            if isinstance(content, str):
                total += len(content)
            for child in obj.values():
                if isinstance(child, (list, dict)):
                    total += self._total_text(child)
            return total
        return 0

    def _components_hash(self, components: List[BaseComponent]) -> str:
        try:
            payload = json.dumps(
                [c.to_dict() for c in components], sort_keys=True, ensure_ascii=False,
            )
        except Exception:
            # Force an edit if we can't hash deterministically.
            payload = repr(random.random())
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _hash_key(self, group_id: int, message_id) -> str:
        return _HASH_KEY_TEMPLATE.format(group_id=group_id, message_id=message_id)

    def _get_stored_hash(self, group_id: int, message_id) -> Optional[str]:
        try:
            existing = redis_client.client.get(self._hash_key(group_id, message_id))
            if not existing:
                return None
            if isinstance(existing, bytes):
                existing = existing.decode("utf-8")
            return existing
        except Exception as e:
            log.debug("HOF: hash read failed for %s/%s: %s", group_id, message_id, e)
            return None

    def _store_hash(self, group_id: int, message_id, new_hash: str):
        try:
            redis_client.client.set(
                self._hash_key(group_id, message_id), new_hash, ex=_HASH_TTL_SECONDS,
            )
        except Exception as e:
            log.warning("HOF: hash store failed for %s/%s: %s", group_id, message_id, e)

    # ------------------------------------------------------------------ #
    # Guild join: clear backoff + reconcile immediately
    # ------------------------------------------------------------------ #

    @listen(GuildJoin)
    async def on_guild_join(self, event: GuildJoin):
        """When the HOF bot is (re-)invited to a guild, retry its group now.

        Without this, a group that fixes the exact problem the backoff was
        punishing (bot missing from the guild) still waits out the remaining
        cooldown — Rancour PvM re-invited the bot and sat through an 8h
        cooldown before anything posted. interactions also fires GuildJoin for
        every guild during the startup sync; a fresh process has no strike
        state, so the strikes-guard makes those a no-op.
        """
        try:
            guild_id = str(event.guild.id)
        except Exception:
            return
        try:
            async with self._work_lock:
                self._begin_fresh_read()
                groups = session.query(Group).filter(Group.guild_id == guild_id).all()
                targets = [
                    g.group_id for g in groups
                    if g.group_id in self._forbidden_until
                    or g.group_id in self._forbidden_strikes
                ]
        except Exception:
            self._safe_rollback()
            log.exception("HOF: guild-join lookup failed for guild %s", guild_id)
            return
        for group_id in targets:
            log.warning(
                "HOF: bot (re)joined guild %s — clearing backoff for group %d "
                "and reconciling now", guild_id, group_id,
            )
            self._clear_forbidden(group_id)
            stats = CycleStats()
            try:
                async with self._work_lock:
                    await asyncio.wait_for(
                        self._reconcile_group(group_id, stats),
                        timeout=_GROUP_TIMEOUT_SECONDS,
                    )
                log.warning("HOF: guild-join reconcile for group %d done | %s",
                            group_id, stats.summary())
            except (Forbidden, ChannelNotPostable) as e:
                self._safe_rollback()
                self._note_forbidden(
                    group_id,
                    reason=str(e) if isinstance(e, ChannelNotPostable) else None,
                    channel_id=getattr(e, "channel_id", None),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._safe_rollback()
                log.exception("HOF: guild-join reconcile failed for group %d", group_id)
        # End whatever read transaction this handler left open on the shared
        # session (the group lookup alone opens one even when targets is empty).
        async with self._work_lock:
            self._safe_rollback()

    # ------------------------------------------------------------------ #
    # Boss-select interaction (ephemeral leaderboards)
    # ------------------------------------------------------------------ #

    @listen(Component)
    async def on_boss_select(self, event: Component):
        ctx: ComponentContext = event.ctx
        group_id = parse_select_custom_id(getattr(ctx, "custom_id", "") or "")
        if group_id is None:
            return
        values = list(getattr(ctx, "values", None) or [])
        if not values:
            return
        selection = str(values[0])
        try:
            await ctx.defer(ephemeral=True)
            session.expire_all()
            group = session.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                await ctx.send("This group no longer exists.", ephemeral=True)
                return
            cfg = self._load_group_config(group_id)
            resolved = await self._resolve_entries(group_id, build_boss_plan(cfg.boss_names))
            match = next(
                ((entry, npcs) for entry, npcs in resolved if entry.display_name == selection),
                None,
            )
            if match is None:
                await ctx.send(
                    f"**{selection}** is no longer part of this group's Hall of Fame.",
                    ephemeral=True,
                )
                return
            entry, npcs = match
            components = self._render_boss_entry(group, entry, npcs, directory_url=None, cfg=cfg)
            if not components:
                await ctx.send(
                    "This leaderboard is too large to display right now.", ephemeral=True,
                )
                return
            await ctx.send(components=components, ephemeral=True)
        except Exception:
            log.exception("HOF: boss select failed (group=%s, selection=%s)", group_id, selection)
            try:
                await ctx.send(
                    "Something went wrong fetching that leaderboard — please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass
        finally:
            # Pure-read handler: end the transaction it autobegan so it can't
            # idle in innodb_trx until the next sweep cycle.
            self._safe_rollback()
