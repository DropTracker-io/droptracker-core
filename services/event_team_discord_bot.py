"""Core-bot reconciler for per-team Discord roles & channels (web53a).

The Discord half of ``services/event_team_discord.py``: drains the
hard-delete orphan queue, then walks the ``web_event_team_discord`` rows the
Web API marked ``pending`` / ``delete_pending`` / ``members_dirty`` and makes
Discord match — create/rename the team role (team name + accent color) and
the team channel (a private text channel bound to the role, or a thread
auto-created inside the configured forum channel), sync role/thread
membership against the materialized roster, and tear everything down on
retirement (immediately for removals; after the 48h grace for a naturally
ended 'delete_48h' event).

Only this module talks to Discord. Failures are isolated per row: a failing
row is marked ``failed`` with ``last_error``, raises a group notice so the
clan's admins actually hear about it, and is re-attempted on an escalating
backoff that tops out at a day (see "Retrying a failed row" below — it used to
be terminal, which quietly cost real events their team channels). Sessions are
opened per tick and always closed (rollback-on-error) — the idle-transaction
lessons apply here too.

Note that ``sync_status`` gates *provisioning*, not *posting*: the board post,
the lootboard and team notifications all key off
``event_team_discord.LIVE_CHANNEL_STATUSES`` instead, so a row that cannot get
its roles right still gets its messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime

from sqlalchemy import and_, or_

TEARDOWN_REASON = "DropTracker event team cleanup"
PROVISION_REASON = "DropTracker event team setup"

# Modest per-tick cap: creations are several REST calls each and Discord
# rate-limits role/channel creation aggressively; a big event drains over a
# few ticks instead of tripping 429 storms.
ROW_LIMIT = 15
# Per-row cap on member add/remove operations per tick; large rosters converge
# over consecutive ticks (members_dirty stays set until the diff is empty).
# Kept modest: the role add/remove bucket is ~5/s, and a big burst of REST
# calls once starved the gateway heartbeat long enough to drop the shard.
MEMBER_OPS_LIMIT = 25

# interactions' interval Tasks have NO overlap guard — a slow pass (rate-limited
# member sync, chromium renders) must not stack a second instance on top of
# itself, which is exactly what exhausted the DB pool on 2026-07-17. Ticks that
# find the lock held just skip; the next tick resumes where the flags left off.
_RECONCILE_LOCK = asyncio.Lock()
_BOARD_POST_LOCK = asyncio.Lock()


def _parse_color(value):
    """'#rrggbb' -> int, or None."""
    try:
        if isinstance(value, str) and value.startswith("#") and len(value) == 7:
            return int(value[1:], 16)
    except ValueError:
        pass
    return None


def _role_color_value(role) -> int:
    """A fetched role's color as a plain int. ``Role.color`` is an
    ``interactions.Color``, which is NOT int()-able (``int(Color)`` raises
    TypeError) — doing that blew up every re-sync of a team that HAD an accent
    color, marking the row 'failed' while colorless teams sailed through."""
    color = getattr(role, "color", None)
    return int(getattr(color, "value", color) or 0)


def _desired_member_ids(session, team_id) -> set:
    """Discord user ids of the team's current roster (players whose owning
    user linked a Discord account)."""
    from db.models import EventTeamMember, Player, User

    rows = (
        session.query(User.discord_id)
        .join(Player, Player.user_id == User.user_id)
        .join(EventTeamMember, EventTeamMember.player_id == Player.player_id)
        .filter(EventTeamMember.team_id == team_id)
        .distinct()
        .all()
    )
    return {str(d) for (d,) in rows if d}


async def drain_team_discord_orphans(bot, redis_client, limit: int = 50) -> None:
    """Delete roles/channels orphaned by a hard delete (event or team). The
    DB rows are already gone; entries are best-effort and failure-isolated."""
    from services.event_team_discord import ORPHAN_TEAM_DISCORD_KEY

    for _ in range(limit):
        raw = redis_client.lpop(ORPHAN_TEAM_DISCORD_KEY)
        if not raw:
            break
        try:
            data = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, AttributeError):
            continue
        await _delete_discord_objects(
            bot, data.get("guild_id"), data.get("role_id"), data.get("channel_id"),
            data.get("voice_channel_id"))


async def _delete_discord_objects(bot, guild_id, role_id, channel_id,
                                  voice_channel_id=None) -> None:
    """Best-effort teardown of one row's channels + role. Missing objects (bot
    kicked, already deleted by hand) just mean nothing left to clean up."""
    for cid in (channel_id, voice_channel_id):
        if not cid:
            continue
        try:
            channel = await bot.fetch_channel(int(cid))
            if channel is not None:
                await channel.delete(reason=TEARDOWN_REASON)
        except Exception:
            pass
    if role_id and guild_id:
        try:
            guild = await bot.fetch_guild(int(guild_id))
            if guild is not None:
                role = await guild.fetch_role(int(role_id))
                if role is not None:
                    await role.delete(reason=TEARDOWN_REASON)
        except Exception:
            pass


async def _ensure_role(guild, row, team) -> None:
    """Create/rename/recolor the team role; writes row.role_id back."""
    import interactions

    color = _parse_color(getattr(team, "color", None))
    role = None
    if row.role_id:
        try:
            role = await guild.fetch_role(int(row.role_id))
        except Exception:
            role = None
    if role is None:
        role = await guild.create_role(
            name=team.name[:100],
            color=color if color is not None else interactions.MISSING,
            mentionable=True,
            reason=PROVISION_REASON,
        )
        row.role_id = str(role.id)
        return
    edits = {}
    if role.name != team.name[:100]:
        edits["name"] = team.name[:100]
    if color is not None and _role_color_value(role) != color:
        edits["color"] = color
    if edits:
        await role.edit(**edits)  # Role.edit takes no audit reason in 5.x


def _channel_intro(event, team) -> str:
    """The team channel/thread intro line, tailored to the event's format —
    a bingo event must not advertise dice rolls it will never send."""
    kind = getattr(event, "kind", None) or "standard"
    if getattr(event, "has_bingo", False):
        what = "tile completions, task progress and lead changes"
    elif kind == "board_game":
        what = "roll prompts, dice results, task progress and lead changes"
    else:
        what = "task progress, completions and lead changes"
    return (f"Team channel for **{team.name}** — {what} land here, and the "
            f"pinned board post below tracks your live progress.")


async def _ensure_channel(bot, guild, row, team, config, event,
                          icon_index: int = 0) -> None:
    """Create/rename the team channel: a thread in the configured forum, or a
    private text channel visible to the team role (public when roles are off).
    Both are named with the team's colored circle up front ("🔵┃blue-team");
    ``icon_index`` is the team's ordinal, used only when the team has no
    accent color to match. Writes row.channel_id / row.channel_kind back."""
    import interactions
    from services.event_team_discord import (
        channel_name_for_team,
        thread_name_for_team,
    )

    forum_id = config.get("forum_channel_id")
    desired_kind = "thread" if forum_id else "text"

    channel = None
    if row.channel_id:
        try:
            channel = await bot.fetch_channel(int(row.channel_id))
        except Exception:
            channel = None
        if channel is not None and row.channel_kind != desired_kind:
            # Mode switched (forum <-> text): replace the old home.
            try:
                await channel.delete(reason=TEARDOWN_REASON)
            except Exception:
                pass
            channel = None
            row.channel_id = None

    if desired_kind == "thread":
        name = thread_name_for_team(team.name, getattr(team, "color", None),
                                    icon_index)
        if channel is None:
            forum = await bot.fetch_channel(int(forum_id))
            if forum is None or not hasattr(forum, "create_post"):
                raise RuntimeError(
                    f"forum channel {forum_id} not found or not a forum")
            post = await forum.create_post(name, _channel_intro(event, team))
            row.channel_id = str(post.id)
            row.channel_kind = "thread"
        elif getattr(channel, "name", None) != name:
            await channel.edit(name=name, reason=PROVISION_REASON)
        return

    name = channel_name_for_team(team.name, getattr(team, "color", None),
                                 icon_index)
    # Optional parent CATEGORY: text channels created inside it inherit its
    # place in the tree AND keep their per-team overwrites, so teams are truly
    # isolated (unlike threads). None = created at guild root, as before.
    category_id = config.get("category_channel_id")
    if channel is None:
        overwrites = []
        if row.role_id:
            # Private to the team: hide from @everyone, show to the team role
            # and to the bot itself (it has to post here).
            overwrites = [
                interactions.PermissionOverwrite(
                    id=int(guild.id),
                    type=interactions.OverwriteType.ROLE,
                    deny=interactions.Permissions.VIEW_CHANNEL,
                ),
                interactions.PermissionOverwrite(
                    id=int(row.role_id),
                    type=interactions.OverwriteType.ROLE,
                    allow=(interactions.Permissions.VIEW_CHANNEL
                           | interactions.Permissions.SEND_MESSAGES),
                ),
                interactions.PermissionOverwrite(
                    id=int(bot.user.id),
                    type=interactions.OverwriteType.MEMBER,
                    allow=(interactions.Permissions.VIEW_CHANNEL
                           | interactions.Permissions.SEND_MESSAGES),
                ),
            ]
        channel = await guild.create_text_channel(
            name=name,
            topic=f"DropTracker team channel — {team.name}",
            category=int(category_id) if category_id else interactions.MISSING,
            permission_overwrites=overwrites or interactions.MISSING,
            reason=PROVISION_REASON,
        )
        row.channel_id = str(channel.id)
        row.channel_kind = "text"
    else:
        if getattr(channel, "name", None) != name:
            await channel.edit(name=name, reason=PROVISION_REASON)
        # Category was added/changed after the channel already existed: move it
        # (a bad/deleted category id must not wedge the sync — best effort).
        if category_id and str(getattr(channel, "parent_id", "") or "") != str(category_id):
            try:
                await channel.edit(parent_id=int(category_id), reason=PROVISION_REASON)
            except Exception as exc:
                print(f"[team-discord] channel {row.channel_id}: could not move "
                      f"to category {category_id}: {exc}")


async def _ensure_voice_channel(bot, guild, row, team, config,
                                icon_index: int = 0) -> None:
    """Create/rename the team's temporary VOICE channel: private to the team
    role when one exists (deny @everyone, allow role view/connect/speak),
    public otherwise — the same access model as the text channel, in the same
    optional category. Writes row.voice_channel_id back."""
    import interactions
    from services.event_team_discord import voice_name_for_team

    name = voice_name_for_team(team.name, getattr(team, "color", None),
                               icon_index)
    channel = None
    if row.voice_channel_id:
        try:
            channel = await bot.fetch_channel(int(row.voice_channel_id))
        except Exception:
            channel = None

    category_id = config.get("category_channel_id")
    if channel is None:
        overwrites = []
        if row.role_id:
            overwrites = [
                interactions.PermissionOverwrite(
                    id=int(guild.id),
                    type=interactions.OverwriteType.ROLE,
                    deny=interactions.Permissions.VIEW_CHANNEL,
                ),
                interactions.PermissionOverwrite(
                    id=int(row.role_id),
                    type=interactions.OverwriteType.ROLE,
                    allow=(interactions.Permissions.VIEW_CHANNEL
                           | interactions.Permissions.CONNECT
                           | interactions.Permissions.SPEAK),
                ),
                interactions.PermissionOverwrite(
                    id=int(bot.user.id),
                    type=interactions.OverwriteType.MEMBER,
                    allow=(interactions.Permissions.VIEW_CHANNEL
                           | interactions.Permissions.CONNECT),
                ),
            ]
        channel = await guild.create_voice_channel(
            name=name,
            category=int(category_id) if category_id else interactions.MISSING,
            permission_overwrites=overwrites or interactions.MISSING,
            reason=PROVISION_REASON,
        )
        row.voice_channel_id = str(channel.id)
    else:
        if getattr(channel, "name", None) != name:
            await channel.edit(name=name, reason=PROVISION_REASON)
        # Category added/changed after creation: move it (best effort — a
        # bad/deleted category id must not wedge the sync).
        if category_id and str(getattr(channel, "parent_id", "") or "") != str(category_id):
            try:
                await channel.edit(parent_id=int(category_id), reason=PROVISION_REASON)
            except Exception as exc:
                print(f"[team-discord] voice channel {row.voice_channel_id}: "
                      f"could not move to category {category_id}: {exc}")


def _friendly_row_error(exc) -> str:
    """Turn a provisioning exception into something a non-technical clan
    leader can act on — raw Discord API strings ("403 Forbidden (error code:
    50013)") read as gibberish in the web UI."""
    try:
        from interactions.client import errors as ix_errors

        if isinstance(exc, ix_errors.Forbidden):
            return ("Discord refused the change (missing permission). Give "
                    "the DropTracker role 'Manage Roles' and 'Manage "
                    "Channels' in Server Settings → Roles, and move it above "
                    "the team roles, then save the Discord settings again.")
        if isinstance(exc, ix_errors.NotFound):
            return ("A configured Discord channel or role no longer exists — "
                    "it may have been deleted. Re-pick it in the Discord "
                    "settings and save again.")
    except ImportError:
        pass
    return f"Unexpected error: {str(exc)[:180]} — fix and re-save to retry."


async def _sync_members(bot, guild, row, desired: set) -> bool:
    """Diff ``desired`` Discord ids against the snapshot the bot last applied
    and add/remove the role + thread membership accordingly. Only ids this
    reconciler added are ever removed — a member an admin tagged by hand is
    never stripped. Returns True when fully converged (may take several ticks
    for large rosters — MEMBER_OPS_LIMIT)."""
    try:
        state = set(json.loads(row.member_state)) if row.member_state else set()
    except (ValueError, TypeError):
        state = set()

    to_add = sorted(desired - state)
    to_remove = sorted(state - desired)
    ops = 0

    thread = None
    if row.channel_id and row.channel_kind == "thread":
        try:
            thread = await bot.fetch_channel(int(row.channel_id))
        except Exception:
            thread = None

    async def _attempt(coro) -> str:
        """One member operation -> 'ok' | 'absent' | 'forbidden' | 'error'.

        Absent-from-guild (404) is EXPECTED for rosters that include people
        outside the server — swallowed and marked handled (they're picked up
        on the next dirty pass after joining). Permission-refused (403) is a
        SERVER MISCONFIGURATION (bot role below the team role — the default
        for fresh roles — or Manage Roles revoked): the id must NOT be marked
        handled, or the whole roster silently never gets its role while the
        UI reads "synced" (audit P0-11); it stays dirty and self-heals the
        pass after an admin fixes the hierarchy. Anything ELSE (network blip,
        5xx, gateway trouble) is transient: also not marked handled — exactly
        what happened when the first sync ran during the 2026-07-17 pool
        outage."""
        from interactions.client import errors as ix_errors

        try:
            await coro
            return "ok"
        except ix_errors.NotFound:
            return "absent"
        except ix_errors.Forbidden:
            return "forbidden"
        except Exception:
            return "error"

    guild_id = int(row.guild_id)
    role_id = int(row.role_id) if row.role_id else None
    had_errors = False
    had_forbidden = False

    for uid in to_add:
        if ops >= MEMBER_OPS_LIMIT:
            break
        results = []
        if role_id is not None:
            # Direct PUT — no member fetch (a fetch-per-id storm across a big
            # roster once rate-limited the bot into a heartbeat drop).
            results.append(await _attempt(bot.http.add_guild_member_role(
                guild_id, int(uid), role_id, reason=PROVISION_REASON)))
        if thread is not None:
            results.append(await _attempt(thread.add_member(int(uid))))
        ops += 1  # every attempted id consumes rate budget, success or not
        if "forbidden" in results:
            had_forbidden = True
            continue  # misconfigured perms — retry once an admin fixes them
        if "error" in results:
            had_errors = True
            continue  # transient — retry on the next members_dirty pass
        # Applied, or definitively absent from the guild: handled either way
        # (absent members get picked up on the next dirty pass after joining).
        state.add(uid)

    for uid in to_remove:
        if ops >= MEMBER_OPS_LIMIT:
            break
        results = []
        if role_id is not None:
            results.append(await _attempt(bot.http.remove_guild_member_role(
                guild_id, int(uid), role_id, reason=TEARDOWN_REASON)))
        if thread is not None:
            results.append(await _attempt(thread.remove_member(int(uid))))
        ops += 1
        if "forbidden" in results:
            had_forbidden = True
            continue  # keep in state; retried once perms are fixed
        if "error" in results:
            had_errors = True
            continue  # keep in state; retried next pass
        state.discard(uid)

    row.member_state = json.dumps(sorted(state))
    if had_forbidden:
        # Actionable, plain-English surface for the web UI (the raw Discord
        # error is useless to a non-technical leader).
        row.last_error = (
            "Discord refused role/member changes (missing permission). In "
            "Server Settings → Roles, move the DropTracker role ABOVE the "
            "team roles and make sure it has 'Manage Roles'. Team pings and "
            "channel access won't work until then.")
    converged = state == desired and not had_errors and not had_forbidden
    if converged and getattr(row, "last_error", None):
        row.last_error = None
    return converged


# --------------------------------------------------------------------------- #
# Retrying a failed row
# --------------------------------------------------------------------------- #
# ``failed`` used to be terminal: the candidate query below never selected it
# again, so the row waited for a human to re-save the event's Discord settings.
# The intent was "no retry storm on a permanent Forbidden", and the storm is
# real — but so is the other half, which nobody was watching: the single most
# common cause of a failed row is the DropTracker role sitting below the team
# roles, an admin fixes that in Discord, and then NOTHING happens, because the
# fix is invisible to us until someone thinks to re-save a settings page they
# have no reason to revisit. Event 58 sat with eight failed rows and not one
# channel created; event 46 lost a team channel for an entire two-week bingo.
#
# So failed rows are retried, on an escalating backoff that reaches a day after
# five attempts — often enough to notice a fix within minutes of it landing,
# rarely enough that a genuinely permanent Forbidden costs a handful of
# requests a day. The backoff lives in Redis rather than a new column: it is
# pure retry bookkeeping with no reporting value, it wants a TTL rather than a
# sweep, and losing it to a restart or a flushed cache just means one early
# retry.
_RETRY_BACKOFF_SECONDS = (300, 900, 3600, 21600, 86400)
# Failed rows are drained on their own small budget, NOT out of ROW_LIMIT: a
# pile of them must never crowd out the pending rows that are someone waiting
# on a channel to appear.
FAILED_RETRY_LIMIT = 5
# How many failed rows to look at to find those FAILED_RETRY_LIMIT (most are
# usually mid-backoff).
FAILED_SCAN_LIMIT = 60


def _retry_gate_key(row_id) -> str:
    return f"events:team_discord:retry:{int(row_id)}"


def _retry_count_key(row_id) -> str:
    return f"events:team_discord:retry:{int(row_id)}:n"


def _raw_redis(redis_client):
    """The raw redis handle, or None. The wrapper has no ttl/incr ops."""
    return getattr(redis_client, "client", None)


def _retry_is_due(redis_client, row_id) -> bool:
    """Whether a failed row's backoff window has elapsed.

    Fails OPEN: if Redis cannot answer, retry. A row that is never retried is
    the failure mode this whole mechanism exists to prevent, and the worst case
    of the opposite is one extra attempt per pass while Redis is down."""
    conn = _raw_redis(redis_client)
    if conn is None:
        return True
    try:
        return not conn.exists(_retry_gate_key(row_id))
    except Exception:
        return True


def _arm_retry_backoff(redis_client, row_id) -> None:
    """Widen and re-arm the backoff after an attempt failed."""
    conn = _raw_redis(redis_client)
    if conn is None:
        return
    try:
        attempt = int(conn.incr(_retry_count_key(row_id)))
        # The counter outlives its own window so consecutive failures actually
        # escalate; a row that comes good clears both keys.
        conn.expire(_retry_count_key(row_id), _RETRY_BACKOFF_SECONDS[-1] * 2)
        delay = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS)) - 1]
        conn.set(_retry_gate_key(row_id), str(attempt), ex=delay)
    except Exception:
        pass  # best effort — a lost backoff costs one early retry, nothing more


def _clear_retry_backoff(redis_client, row_id) -> None:
    """Forget a row's failure history — it provisioned cleanly, or an admin
    re-saved the settings and has earned an immediate attempt."""
    conn = _raw_redis(redis_client)
    if conn is None:
        return
    try:
        conn.delete(_retry_gate_key(row_id), _retry_count_key(row_id))
    except Exception:
        pass


def _take_failed_rows_for_retry(session, redis_client) -> list:
    """Failed rows whose backoff has elapsed, oldest first, capped at
    FAILED_RETRY_LIMIT — arming each one's NEXT backoff window as it is taken.

    Arming up front rather than in the failure handler is deliberate: it makes
    the gate hold even if this pass never reaches its own error path (the
    process is restarted mid-retry, a row is left ``pending`` and re-enters
    through the ordinary query). Without that, a crash loop during a retry is
    an unthrottled retry loop. A row that provisions cleanly clears it."""
    from db.models import EventTeamDiscord

    try:
        candidates = (
            session.query(EventTeamDiscord)
            .filter(EventTeamDiscord.sync_status == "failed")
            .order_by(EventTeamDiscord.id.asc())
            .limit(FAILED_SCAN_LIMIT)
            .all()
        )
    except Exception as exc:  # noqa: BLE001 — a scan failure must not end the pass
        session.rollback()
        print(f"[team-discord] failed-row scan failed: {exc}")
        return []
    due = []
    for row in candidates:
        if not _retry_is_due(redis_client, row.id):
            continue
        _arm_retry_backoff(redis_client, row.id)
        due.append(row)
        if len(due) >= FAILED_RETRY_LIMIT:
            break
    return due


# --------------------------------------------------------------------------- #
# Telling the group about it
# --------------------------------------------------------------------------- #

TEAM_DISCORD_NOTICE_CODE = "event_team_discord_failed"


def _notice_group_id(event, row):
    """The clan a team-channel problem belongs to: the per-clan scope in a
    clan-vs-clan event, otherwise the event's owning group."""
    return getattr(row, "group_id", None) or getattr(event, "group_id", None)


def _raise_team_discord_notice(session, event, row, team) -> None:
    """Tell the clan's admins that a team channel is not provisioning.

    ``last_error`` has always carried an actionable, plain-English sentence —
    it was just never pushed anywhere, so it sat on a row nobody opens while a
    team channel stayed broken for two weeks. group_notices is the surface
    built for exactly this: one row per (group, code), a chat thread the
    admins already see, a DM on the open transition only, and a 24h cooldown
    so a retry loop cannot turn into a spam loop."""
    group_id = _notice_group_id(event, row)
    if not group_id:
        return
    try:
        from services.group_notices import raise_group_notice

        team_name = getattr(team, "name", None) or f"team {row.team_id}"
        raise_group_notice(
            session,
            group_id=int(group_id),
            code=TEAM_DISCORD_NOTICE_CODE,
            title="Event team channels aren't setting up",
            body=(f"DropTracker couldn't finish setting up the Discord role "
                  f"and channel for **{team_name}** in **"
                  f"{getattr(event, 'name', 'your event')}**.\n\n"
                  f"{row.last_error}\n\n"
                  f"We'll keep retrying on our own, so once the permission is "
                  f"fixed this sorts itself out — no need to re-save "
                  f"anything."),
            severity="major",
            data={"event_id": getattr(event, "id", None),
                  "team_id": row.team_id,
                  "guild_id": str(row.guild_id)},
        )
    except Exception as exc:  # noqa: BLE001 — never break a sync over a notice
        print(f"[team-discord] could not raise notice for group {group_id}: {exc}")


def _resolve_team_discord_notice(session, event, row) -> None:
    """Close the notice once this group has NO team row still in trouble.

    Per-row resolution would flap: a five-team event with one broken role
    would open and close the same notice every pass."""
    group_id = _notice_group_id(event, row)
    if not group_id:
        return
    try:
        from db.models import Event, EventTeamDiscord
        from services.group_notices import resolve_group_notice

        still_broken = (
            session.query(EventTeamDiscord.id)
            .join(Event, Event.id == EventTeamDiscord.event_id)
            .filter(EventTeamDiscord.id != row.id,
                    or_(EventTeamDiscord.group_id == int(group_id),
                        and_(EventTeamDiscord.group_id.is_(None),
                             Event.group_id == int(group_id))),
                    or_(EventTeamDiscord.sync_status == "failed",
                        EventTeamDiscord.last_error.isnot(None)))
            .first()
        )
        if still_broken is None:
            resolve_group_notice(session, group_id=int(group_id),
                                 code=TEAM_DISCORD_NOTICE_CODE)
    except Exception as exc:  # noqa: BLE001
        print(f"[team-discord] could not resolve notice for group {group_id}: {exc}")


async def reconcile_event_team_discord_once(bot, session_factory, redis_client) -> None:
    """One reconcile pass (called from the bots/main.py interval task).
    Skips entirely when the previous pass is still running (see
    ``_RECONCILE_LOCK``)."""
    if _RECONCILE_LOCK.locked():
        return
    async with _RECONCILE_LOCK:
        await _reconcile_pass(bot, session_factory, redis_client)


async def _reconcile_pass(bot, session_factory, redis_client) -> None:
    from db.models import Event, EventTeam, EventTeamDiscord
    from services.event_team_discord import (
        team_discord_scopes,
        team_flags,
        team_icon_index,
    )

    await drain_team_discord_orphans(bot, redis_client)

    session = session_factory()
    try:
        now = datetime.now()
        rows = (
            session.query(EventTeamDiscord)
            .filter(or_(
                EventTeamDiscord.sync_status.in_(("pending", "delete_pending")),
                and_(EventTeamDiscord.sync_status == "synced",
                     EventTeamDiscord.members_dirty.is_(True)),
            ))
            .order_by(EventTeamDiscord.id.asc())
            .limit(ROW_LIMIT)
            .all()
        )
        # Failed rows whose backoff has elapsed ride along on their own budget,
        # re-run as if pending: the whole point is to re-attempt the
        # role/channel creation that failed in the first place. Their next
        # window is armed as they are taken, so the failure handler below must
        # not arm it a second time and escalate at double speed.
        retry_ids = set()
        for row in _take_failed_rows_for_retry(session, redis_client):
            row.sync_status = "pending"
            retry_ids.add(row.id)
            rows.append(row)
        # Scope configs are per event — cache across rows of the same event.
        scopes_cache: dict = {}
        for row in rows:
            event = team = None
            try:
                if row.sync_status == "delete_pending":
                    if row.delete_after and now < row.delete_after:
                        continue  # 48h grace still running
                    await _delete_discord_objects(
                        bot, row.guild_id, row.role_id, row.channel_id,
                        row.voice_channel_id)
                    session.delete(row)
                    session.commit()
                    continue

                event = session.query(Event).filter(Event.id == row.event_id).first()
                team = (session.query(EventTeam)
                        .filter(EventTeam.id == row.team_id).first())
                if event is None or team is None:
                    session.delete(row)
                    session.commit()
                    continue
                if event.id not in scopes_cache:
                    scopes_cache[event.id] = team_discord_scopes(session, event)
                scope = next(
                    (sc for sc in scopes_cache[event.id]
                     if sc["group_id"] == row.group_id
                     and sc["guild_id"] == str(row.guild_id)),
                    None,
                )
                if scope is None:
                    # Config gone / guild re-pointed since the row was made.
                    row.sync_status = "delete_pending"
                    row.delete_after = None
                    session.commit()
                    continue
                flags = team_flags(scope["config"], team.id)

                guild = await bot.fetch_guild(int(row.guild_id))
                if guild is None:
                    raise RuntimeError("bot is not a member of this guild")

                if row.sync_status == "pending":
                    if flags["role"]:
                        await _ensure_role(guild, row, team)
                    elif row.role_id:
                        await _delete_discord_objects(bot, row.guild_id, row.role_id, None)
                        row.role_id = None
                    if flags["channel"]:
                        await _ensure_channel(
                            bot, guild, row, team, scope["config"], event,
                            icon_index=team_icon_index(session, event.id, team.id))
                    elif row.channel_id:
                        await _delete_discord_objects(bot, None, None, row.channel_id)
                        row.channel_id = None
                        row.channel_kind = None
                    if flags["voice"]:
                        await _ensure_voice_channel(
                            bot, guild, row, team, scope["config"],
                            icon_index=team_icon_index(session, event.id, team.id))
                    elif row.voice_channel_id:
                        await _delete_discord_objects(bot, None, None,
                                                      row.voice_channel_id)
                        row.voice_channel_id = None
                    row.sync_status = "synced"
                    row.synced_at = now
                    row.last_error = None
                    row.members_dirty = True
                    session.commit()
                    # Provisioning came good: forget the failure history and
                    # close the group's notice if this was its last bad row.
                    _clear_retry_backoff(redis_client, row.id)
                    _resolve_team_discord_notice(session, event, row)
                    try:
                        from services.event_team_discord import TEAM_BOARD_DIRTY_KEY

                        redis_client.client.sadd(TEAM_BOARD_DIRTY_KEY, str(row.event_id))
                    except Exception:
                        pass

                if row.members_dirty:
                    desired = _desired_member_ids(session, row.team_id)
                    # End the read transaction BEFORE the (rate-limited) REST
                    # phase — holding it open across minutes of member calls
                    # is the idle-in-transaction pathology that starved the
                    # pool on 2026-07-17. The write-back below starts fresh.
                    session.commit()
                    converged = await _sync_members(bot, guild, row, desired)
                    row.members_dirty = not converged
                    session.commit()
                    # _sync_members swallows per-member Forbiddens and reports
                    # them through last_error rather than raising, so this is
                    # the only place a role-hierarchy problem becomes visible.
                    if row.last_error:
                        _raise_team_discord_notice(session, event, row, team)
                    else:
                        _resolve_team_discord_notice(session, event, row)
            except Exception as exc:  # noqa: BLE001 — isolate per row
                session.rollback()
                try:
                    row.sync_status = "failed"
                    row.last_error = _friendly_row_error(exc)
                    session.commit()
                    if row.id not in retry_ids:
                        _arm_retry_backoff(redis_client, row.id)
                    if event is not None:
                        _raise_team_discord_notice(session, event, row, team)
                except Exception:
                    session.rollback()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Team-channel primary board posts (web54a)
# --------------------------------------------------------------------------- #
# Screenshot budget per refresher tick: renders are headless-chromium runs, so
# a burst of changes across many teams converges over a few ticks instead of
# stalling the bot's event loop. Rows skipped on budget stay flagged dirty.
BOARD_POST_RENDER_BUDGET = 6
# How many dirty event ids one tick drains from Redis.
BOARD_DIRTY_SPOP_LIMIT = 10


# --------------------------------------------------------------------------- #
# Editing a message we posted earlier
# --------------------------------------------------------------------------- #
# Both long-lived team-channel messages (the primary board post and the
# lootboard beneath it) are posted once and edited forever after, which makes
# "can I still reach the message I posted?" the load-bearing question of both
# passes. There are exactly two answers, and they demand opposite actions:
#
#   * Discord says the message is GONE (404). Someone deleted it; reposting is
#     the only way back and the id we hold is worthless.
#   * The call did not get through — Forbidden, 5xx, a timeout, a reset
#     connection. The message is almost certainly still sitting there.
#     Reposting FORKS it: the old copy stays in the channel frozen forever
#     while we quietly start editing a new one.
#
# Collapsing those two into `except Exception: repost` is what happened to
# event 46 on 2026-08-19. The fork is not even self-correcting: the
# replacement lands at the BOTTOM of a busy channel while the frozen original
# keeps the position (and, there, the pin) everyone already knows, so the
# team spent the last twelve days of a two-week bingo reading a lootboard
# stuck at 611M while the bot dutifully updated an invisible one to 4.8B.
#
# Hence the tri-state below, and hence: never repost without first making sure
# the thing being replaced is actually gone.

_EDIT_OK = "edited"
_EDIT_MISSING = "missing"          # confirmed 404 — a repost is correct
_EDIT_UNAVAILABLE = "unavailable"  # could not tell — a repost would fork it


def _is_not_found(exc) -> bool:
    """Whether an exception means "this object no longer exists" (404).

    Deliberately narrow: everything it does NOT recognise is treated as a
    transient failure, because the cost of guessing wrong in that direction is
    one retry a minute later, and the cost of guessing wrong in the other is a
    permanently forked message."""
    try:
        from interactions.client import errors as ix_errors

        if isinstance(exc, ix_errors.NotFound):
            return True
    except ImportError:
        pass
    return getattr(exc, "status", None) == 404


async def _edit_tracked_message(channel, message_id, components, files):
    """Edit a message this code posted earlier. Returns ``(message, outcome)``
    with outcome one of :data:`_EDIT_OK` / :data:`_EDIT_MISSING` /
    :data:`_EDIT_UNAVAILABLE` — see the section comment above.

    Note that interactions' ``Channel.fetch_message`` answers a 404 by
    returning None rather than raising, so "gone" arrives on the value path and
    every *exception* here is by definition something other than a deletion.
    The edit itself is inside the same envelope: a message can vanish between
    the fetch (which may be answered from cache) and the write."""
    try:
        message = await channel.fetch_message(message_id=message_id)
    except Exception:  # noqa: BLE001 — Forbidden / 5xx / network
        return None, _EDIT_UNAVAILABLE
    if message is None:
        return None, _EDIT_MISSING
    try:
        if files is not None:
            # attachments=[] drops the previous upload so files don't accumulate.
            await message.edit(components=components, files=files,
                               attachments=[])
        else:
            await message.edit(components=components, attachments=[])
    except Exception as exc:  # noqa: BLE001
        return None, (_EDIT_MISSING if _is_not_found(exc) else _EDIT_UNAVAILABLE)
    return message, _EDIT_OK


async def _delete_bot_message(channel, message_id) -> None:
    """Best-effort delete of a message THIS code posted — the only messages it
    ever deletes. Used to re-seat the lootboard under a re-created board post,
    and before every repost so a replacement can never fork the original."""
    try:
        stale = await channel.fetch_message(message_id=message_id)
        if stale is not None:
            await stale.delete()
    except Exception:
        pass  # already gone / no permission — the repost still has to happen


async def _repost_tracked_message(channel, stale_message_id, components, files):
    """Send a replacement for a message that could not be edited, making sure
    the one it replaces is gone first.

    The delete is the whole point: without it a repost leaves the old copy in
    the channel, and the old copy is the one with the history, the position and
    (for the board post) the pin."""
    if stale_message_id:
        await _delete_bot_message(channel, stale_message_id)
    if files is not None:
        return await channel.send(components=components, files=files)
    return await channel.send(components=components)


def _team_post_payload(session, event, row, team, channel, png):
    """(components, file) for one team's primary board post: header, the
    live team-filtered board image, a pointer at the event's standings
    channel, and interactive/site links."""
    import io

    import interactions

    from services.activity_launch import channel_supports_launch
    from services.event_message_layouts import (
        build_components,
        deeplink_enabled,
        render_message_spec,
    )
    from services.event_notifications import (
        event_footer_line,
        event_url,
        load_event_channels,
    )

    channels = load_event_channels(session, event.id, group_id=row.group_id) \
        if row.group_id is not None else load_event_channels(session, event.id)
    if row.group_id is not None and not channels:
        # web48a semantics: a clan without its own channel set falls back to
        # the event's shared destinations.
        channels = load_event_channels(session, event.id)
    standings_channel = channels.get("leaderboard") or channels.get("announcements")
    if standings_channel and str(standings_channel) == str(row.channel_id):
        standings_channel = None  # don't point the line at this very channel

    context = {
        "event_id": event.id,
        "event_name": event.name,
        "event_url": event_url(event.id),
        "team_name": team.name,
        "standings_line": (f"\U0001F4CA Live standings & announcements: "
                           f"<#{standings_channel}>" if standings_channel else None),
    }
    layout = {"blocks": [
        {"type": "text", "content": "## {team_name} — {event_name}"},
        {"type": "text",
         "content": "-# Your team's live board — it updates automatically "
                    "as progress lands."},
        {"type": "text", "content": "{standings_line}"},
        {"type": "buttons", "buttons": [
            {"label": "\U0001F4F2 Open interactive event", "launch": True},
            {"label": "\U0001F310 View on the site", "url": "{event_url}"},
        ]},
    ]}
    enabled, supported = deeplink_enabled(), channel_supports_launch(channel)
    starts = int(event.starts_at.timestamp()) if event.starts_at else None
    ends = int(event.ends_at.timestamp()) if event.ends_at else None
    spec = render_message_spec(
        layout, context,
        deep_link=enabled and supported,
        launch_link=enabled and not supported,
        footer=event_footer_line(event.name, starts, ends),
    )
    board_file, image_ref = None, None
    if png:
        filename = f"team-board-{event.id}-{row.team_id}.png"
        board_file = interactions.File(io.BytesIO(png), file_name=filename)
        image_ref = f"attachment://{filename}"
    return build_components(spec, image_ref=image_ref), board_file


async def _refresh_one_board_post(bot, session, event, row):
    """Post/edit one team channel's primary board post. Returns
    ``(wrote, rendered)`` — whether a Discord write happened, and whether a
    real screenshot ran (the tick budget counts the latter)."""
    from db.models import EventTeam
    from services.event_board_image import board_image_with_hash

    team = session.query(EventTeam).filter(EventTeam.id == row.team_id).first()
    if team is None:
        return False, False
    # Release the read transaction before the slow parts (chromium render,
    # Discord upload) — same idle-in-transaction hygiene as the reconciler.
    session.commit()
    png, state_hash, rendered = await board_image_with_hash(
        session, event, team_id=row.team_id)
    if state_hash is None:
        return False, rendered  # no visual board (standard event) / render failure
    if row.board_message_id and row.board_state_hash == state_hash:
        return False, rendered  # unchanged — no edit
    channel = await bot.fetch_channel(int(row.channel_id))
    if channel is None or not callable(getattr(channel, "send", None)):
        return False, rendered

    components, board_file = _team_post_payload(
        session, event, row, team, channel, png)

    outcome = _EDIT_MISSING
    if row.board_message_id:
        _, outcome = await _edit_tracked_message(
            channel, row.board_message_id, components, board_file)
        if outcome == _EDIT_UNAVAILABLE:
            # Could not reach the post, and Discord did not say it is gone.
            # Reposting here would fork it — and this one is PINNED, so the
            # channel would end up with two pinned board posts, the stale one
            # first. Change nothing and try again on a later tick.
            return False, rendered
    if outcome == _EDIT_MISSING:
        message = await _repost_tracked_message(
            channel, row.board_message_id, components, board_file)
        row.board_message_id = str(message.id)
        try:
            await message.pin()
        except Exception:
            pass  # pinning is a nicety; missing Manage Messages must not fail the post
    row.board_state_hash = state_hash
    row.board_updated_at = datetime.now()
    return True, rendered


async def refresh_team_board_posts_once(bot, session_factory, redis_client,
                                        render_budget: int = BOARD_POST_RENDER_BUDGET
                                        ) -> None:
    """One refresher tick (bots/main.py, ~60s): drain the dirty-event set the
    engine feeds on every event frame, re-check those events' team posts, and
    edit only the ones whose team-filtered board actually changed
    (``board_state_hash``). Also catches up rows that never got their primary
    post (fresh provisioning, or a post that failed once). Skips entirely
    while the previous tick is still running (chromium renders are slow)."""
    if _BOARD_POST_LOCK.locked():
        return
    async with _BOARD_POST_LOCK:
        await _board_post_pass(bot, session_factory, redis_client, render_budget)
        # The lootboard post rides the same tick (it is the board post's
        # neighbour and must land after it), but it is strictly downstream:
        # its own try/except means a lootboard problem can never regress the
        # primary board post above.
        try:
            await _loot_post_pass(bot, session_factory)
        except Exception as exc:  # noqa: BLE001 — never break the board pass
            print(f"[team-loot] pass failed: {exc}")


async def _board_post_pass(bot, session_factory, redis_client,
                           render_budget: int) -> None:
    from db.models import Event, EventTeamDiscord
    from services.event_team_discord import (
        LIVE_CHANNEL_STATUSES,
        TEAM_BOARD_DIRTY_KEY,
    )

    event_ids: set = set()
    try:
        for _ in range(BOARD_DIRTY_SPOP_LIMIT):
            # .client: raw redis — the wrapper has no set ops (an AttributeError
            # in here once no-oped the whole refresher silently).
            raw = redis_client.client.spop(TEAM_BOARD_DIRTY_KEY)
            if not raw:
                break
            try:
                event_ids.add(int(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw))
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        print(f"[team-board] dirty-set drain failed: {exc}")

    session = session_factory()
    try:
        # First-post catch-up (no dirty flag needed): synced channels with no
        # primary post yet.
        try:
            for (eid,) in (session.query(EventTeamDiscord.event_id)
                           .filter(EventTeamDiscord.sync_status.in_(
                                       LIVE_CHANNEL_STATUSES),
                                   EventTeamDiscord.channel_id.isnot(None),
                                   EventTeamDiscord.board_message_id.is_(None))
                           .distinct().limit(5).all()):
                event_ids.add(int(eid))
        except Exception:
            session.rollback()
        if not event_ids:
            return

        rendered = 0
        for event_id in sorted(event_ids):
            event = session.query(Event).filter(Event.id == event_id).first()
            if event is None:
                continue
            rows = (session.query(EventTeamDiscord)
                    .filter(EventTeamDiscord.event_id == event_id,
                            EventTeamDiscord.sync_status.in_(
                                LIVE_CHANNEL_STATUSES),
                            EventTeamDiscord.channel_id.isnot(None))
                    .order_by(EventTeamDiscord.id.asc())
                    .all())
            for row in rows:
                if rendered >= render_budget:
                    # Out of screenshot budget — keep the event flagged and
                    # finish next tick (hash skips make redone rows cheap).
                    try:
                        redis_client.client.sadd(TEAM_BOARD_DIRTY_KEY, str(event_id))
                    except Exception:
                        pass
                    return
                try:
                    wrote, did_render = await _refresh_one_board_post(
                        bot, session, event, row)
                    rendered += 1 if did_render else 0
                    if wrote:
                        session.commit()
                except Exception as exc:
                    session.rollback()
                    print(f"[team-board] post refresh failed "
                          f"(event {event_id}, team {row.team_id}): {exc}")
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Team-channel lootboard posts (web93a)
# --------------------------------------------------------------------------- #
# The second message in a team channel: the team's event lootboard PNG, posted
# directly beneath the primary board post and edited in place forever after, so
# the pair reads as one continuously-updated block.
#
# The image itself is NOT produced here. ``lootboard/team_boards.py`` renders it
# in a different process (``droptracker-lootboards``, mtime-throttled to one
# render per team per hour) onto the shared ``/img`` tree; this pass only
# delivers whatever is on disk. Consequences that shape the code below:
#   * the PNG is frequently absent (feature off, generator hasn't reached this
#     team yet, private event) — that is a normal state, never an error;
#   * it is rewritten on a timer rather than on change, so an unchanged image
#     must be recognised by CONTENT (``loot_state_hash``), not by mtime alone;
#   * it is delivered as an ATTACHMENT, not as its public /img URL: media
#     galleries fed external URLs spin forever in the Discord client (see
#     services/event_board_image.py).
#
# Discord round trips — not disk reads — are the expensive part, so the tick
# bounds ATTEMPTS, not rows: at most LOOT_POST_WRITE_BUDGET Discord operations
# per 60s tick (~300/hour, enough to converge a hundred teams inside 20 minutes
# on a cold rollout without ever bursting). A failed attempt costs the same
# round trip as a successful one — and a 403 additionally counts toward
# Discord's invalid-request ban budget — so failures are charged too, and the
# row is stamped so it rotates to the BACK of the stalest-first queue instead
# of leading it again 60 seconds later. Without both halves a systematically
# broken channel (bot lost View Channel, channel deleted while the row still
# reads 'synced', missing Attach Files) would re-issue up to
# LOOT_POST_SCAN_LIMIT requests every single tick, forever.
LOOT_POST_WRITE_BUDGET = 5
# Ceiling on rows examined per tick (a stat() each). Only rows whose event is
# still active are candidates, so this is far above any real deployment.
LOOT_POST_SCAN_LIMIT = 200


def _png_mtime(path: str):
    """Modification time of a generated board, or None when it isn't there."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# Every complete PNG ends with its zero-length IEND chunk; a file caught
# mid-encode does not.
_PNG_EOF = b"IEND\xaeB`\x82"


def _read_png(path: str):
    """``(bytes, mtime)`` of a COMPLETE board image, or ``(None, None)``.

    The generator runs in another process (``droptracker-lootboards``) and
    rewrites this file IN PLACE — ``Image.save(path)``: no temp file, no
    rename, no lock — so a plain read can land mid-encode and come back with a
    truncated image. That is worse here than a torn frame would normally be,
    because the bytes we read are hashed into ``loot_state_hash`` and stamped
    into ``loot_updated_at``: a broken picture would not just be uploaded, it
    would be cached as 'delivered' and frozen in the team channel until the
    next hourly regeneration.

    So both ends are checked: the file must be unchanged (size + mtime) across
    the read, AND it must carry PNG's end-of-image marker — size/mtime alone
    are stable in the gap between two of the writer's buffer flushes. A
    rejected read is not an error and is never logged; the next tick simply
    re-reads the finished file.
    """
    try:
        before = os.stat(path)
        with open(path, "rb") as handle:
            data = handle.read()
        after = os.stat(path)
    except OSError:
        return None, None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None, None  # rewritten underneath us
    if len(data) != after.st_size or not data.endswith(_PNG_EOF):
        return None, None  # truncated / still being encoded
    return data, after.st_mtime


def _delivered_stamp(mtime: float) -> datetime:
    """The ``loot_updated_at`` value meaning "THIS file has been delivered".

    Deliberately the delivered file's own mtime rather than ``now()``: the
    mtime skip below asks "has the PNG been rewritten since we delivered it?",
    and a wall-clock stamp answers "no" for a regeneration that landed between
    our read and our stamp — silently discarding the newer image for an hour.
    A file's mtime cannot outrun the file.
    """
    return datetime.fromtimestamp(mtime)


def _loot_state_hash(png: bytes, title: str) -> str:
    """Content signature of what the lootboard message would show. Equal hash
    means an identical image + heading, which means no Discord call."""
    digest = hashlib.sha256()
    digest.update(png or b"")
    digest.update(b"\x00")
    digest.update((title or "").encode("utf-8", "replace"))
    return digest.hexdigest()


def _loot_post_is_below_board(row) -> bool:
    """Whether our lootboard message still sits UNDER the primary board post.

    Discord orders a channel by message id (snowflakes are time-ordered), so
    the answer is pure arithmetic — no API call. It only ever flips to False
    when the board post is re-created (someone deleted it and
    ``_refresh_one_board_post`` sent a new one, with a newer/larger id), which
    would leave our older lootboard stranded ABOVE it.

    Unparseable/absent ids answer True: this decides whether a message gets
    deleted, so it never guesses.
    """
    try:
        return int(row.loot_message_id) > int(row.board_message_id)
    except (TypeError, ValueError):
        return True


def _team_loot_payload(event, row, team, png):
    """``(components, file)`` for one team's lootboard message.

    Deliberately minimal: a heading naming the team + event and the board
    image as an attachment. No links or footer — the primary board post
    immediately above already carries them — and no ``content=``, which V2
    component messages cannot have (the heading is a text display)."""
    import io

    import interactions

    from lootboard.team_boards import board_title
    from services.event_message_layouts import build_components, render_message_spec

    layout = {"blocks": [
        {"type": "text", "content": "## \U0001F4B0 {board_title} — team loot"},
        {"type": "text",
         "content": "-# Every drop your team has banked during this event. "
                    "This image is regenerated about once an hour."},
    ]}
    spec = render_message_spec(
        layout, {"board_title": board_title(event, team)}, deep_link=False)
    filename = f"team-loot-{event.id}-{row.team_id}.png"
    loot_file = interactions.File(io.BytesIO(png), file_name=filename)
    return build_components(spec, image_ref=f"attachment://{filename}"), loot_file


async def _refresh_one_loot_post(bot, session, event, row):
    """Post/edit one team channel's lootboard message.

    Returns ``(wrote, attempted)`` — whether the row changed (the caller
    commits it) and whether a Discord round trip was ATTEMPTED. The tick
    budget counts attempts, not successes: a fetch/send that comes back 403 or
    404 costs the same request as one that works, so a broken row must spend
    budget exactly like a healthy one (see LOOT_POST_WRITE_BUDGET). Every
    failure path that returns rather than raises also stamps
    ``loot_updated_at``, which is what sends the row to the back of the
    stalest-first queue instead of straight back to the front."""
    from db.models import EventTeam
    from lootboard.team_boards import board_group_id, board_title, team_board_path

    if not row.board_message_id:
        # "Directly beneath" is a matter of post ORDER, and Discord orders by
        # message id. The lootboard therefore may never be created before the
        # board post exists — it lands on this pass or the next one, once
        # _refresh_one_board_post has sent the post it belongs under.
        return False, False

    team = session.query(EventTeam).filter(EventTeam.id == row.team_id).first()
    if team is None:
        return False, False

    path = team_board_path(board_group_id(event, team), event.id, row.team_id)
    mtime = _png_mtime(path)
    if mtime is None:
        # No image yet (or ever): the generator is another process on an hourly
        # throttle and the feature may be off for it. Silent by design — this
        # runs every 60s per team and must not log a word about it.
        return False, False

    below = _loot_post_is_below_board(row)
    # The stamp IS the delivered file's mtime (see _delivered_stamp), so this
    # reads "the PNG has not been rewritten since we delivered it". The +1s
    # slack absorbs MySQL DATETIME's second granularity; a genuine regeneration
    # (hourly at most) is never inside it.
    if (row.loot_message_id and below and row.loot_updated_at
            and mtime <= row.loot_updated_at.timestamp() + 1.0):
        # Nothing has rewritten the PNG since we last delivered it: no file
        # read, no hash, no Discord call. This is the overwhelmingly common tick.
        return False, False

    # Release the read transaction before the slow half (disk read + upload) —
    # same idle-in-transaction hygiene as the board post above.
    session.commit()
    png, png_mtime = _read_png(path)
    if not png:
        # Absent, unreadable, or caught mid-encode by the other process. Change
        # nothing (least of all the stamps — a torn image must never be cached
        # as delivered) and re-read the finished file on the next tick.
        return False, False
    state_hash = _loot_state_hash(png, board_title(event, team))
    if row.loot_message_id and below and row.loot_state_hash == state_hash:
        # The generator rewrites on a timer, not on change, so a fresh mtime
        # usually carries an identical image. Stamp the row (the mtime skip
        # above then covers the next hour of ticks) and touch nothing else.
        row.loot_updated_at = _delivered_stamp(png_mtime)
        return True, False

    channel = await bot.fetch_channel(int(row.channel_id))
    if channel is None or not callable(getattr(channel, "send", None)):
        # Gone (deleted while the row still reads 'synced') or invisible to the
        # bot — smart_cache swallows the 403 and hands back a channel object we
        # cannot send through. Either way the fetch was a real request: charge
        # it and stamp the row so this permanently-broken channel rotates to the
        # back of the queue rather than leading every tick.
        row.loot_updated_at = datetime.now()
        return True, True

    components, loot_file = _team_loot_payload(event, row, team, png)

    outcome = _EDIT_MISSING
    if row.loot_message_id and below:
        _, outcome = await _edit_tracked_message(
            channel, row.loot_message_id, components, loot_file)
        if outcome == _EDIT_UNAVAILABLE:
            # The message could not be reached and Discord never said it was
            # deleted. Reposting would leave the original sitting in the
            # channel frozen — and since the lootboard is not pinned, the
            # frozen copy is the one the team keeps finding. Spend the attempt,
            # stamp the row so it goes to the back of the queue, and edit the
            # message we already have on a later tick.
            row.loot_updated_at = datetime.now()
            return True, True
    elif row.loot_message_id:
        # The board post was re-created, so it now sits UNDER our lootboard.
        # Drop ours (we own it) and repost it beneath the new post to keep the
        # pair in order.
        await _delete_bot_message(channel, row.loot_message_id)
        row.loot_message_id = None

    if outcome == _EDIT_MISSING:
        message = await _repost_tracked_message(
            channel, row.loot_message_id, components, loot_file)
        row.loot_message_id = str(message.id)
        # NOT pinned: the board post above is the channel's pinned message, and
        # a second pin would bury it in the pins list.
    row.loot_state_hash = state_hash
    row.loot_updated_at = _delivered_stamp(png_mtime)
    return True, True


def _stamp_loot_backoff(session, row) -> None:
    """After a failed delivery attempt, push the row to the BACK of the queue.

    The rollback that precedes this discards whatever the failed attempt set,
    which would leave ``loot_updated_at`` at its old (usually NULL) value —
    and NULL sorts FIRST in the stalest-first ordering. A handful of
    permanently broken channels would therefore monopolise every tick's budget
    forever, re-uploading the same images into 403s. Stamping the attempt
    makes a failure rotate exactly like a success: retried on a later tick,
    and (once the row has a message) not before the next hourly regeneration
    changes the PNG. Same doctrine as the reconciler above — no retry storm on
    a permanent Forbidden.

    Best-effort: if even this write fails the DB is the problem, and the
    candidate query will fail next tick anyway.
    """
    try:
        row.loot_updated_at = datetime.now()
        session.commit()
    except Exception:  # noqa: BLE001 — never mask the original failure
        session.rollback()


def _loot_candidate_rows(session):
    """``[(row, event)]`` for every team channel that already has its primary
    board post and whose event is still running, stalest delivery first.

    Rows are ordered by ``loot_updated_at`` (NULL — never delivered — first),
    so a cold start converges front-to-back and a delivered row rotates to the
    end of the queue."""
    from db.models import Event, EventTeamDiscord
    from services.event_team_discord import LIVE_CHANNEL_STATUSES

    return (session.query(EventTeamDiscord, Event)
            .join(Event, Event.id == EventTeamDiscord.event_id)
            .filter(EventTeamDiscord.sync_status.in_(LIVE_CHANNEL_STATUSES),
                    EventTeamDiscord.channel_id.isnot(None),
                    EventTeamDiscord.board_message_id.isnot(None),
                    Event.status == "active")
            .order_by(EventTeamDiscord.loot_updated_at.asc(),
                      EventTeamDiscord.id.asc())
            .limit(LOOT_POST_SCAN_LIMIT)
            .all())


async def _loot_post_pass(bot, session_factory,
                          write_budget: int = LOOT_POST_WRITE_BUDGET) -> None:
    """One lootboard-delivery pass, run right after the board-post pass.

    ``write_budget`` bounds Discord ATTEMPTS, not successes: a row that fails
    spends budget and is stamped (``_stamp_loot_backoff``) so it rotates to the
    back of the stalest-first queue. Both halves are load-bearing — the whole
    pass holds ``_BOARD_POST_LOCK``, so an unbounded retry storm here would
    also starve the primary board-post refresh.

    A complete no-op while ``EVENT_TEAM_LOOTBOARDS`` is off: the flag is
    checked before a session is even opened."""
    from lootboard.team_boards import event_is_public, feature_enabled

    if not feature_enabled():
        return

    session = session_factory()
    try:
        try:
            candidates = _loot_candidate_rows(session)
        except Exception as exc:
            session.rollback()
            print(f"[team-loot] candidate query failed: {exc}")
            return

        attempts = 0
        for row, event in candidates:
            if not event_is_public(event):
                # Private events are never rendered to the public /img tree, so
                # there is nothing to deliver (and nothing to leak).
                continue
            try:
                wrote, attempted = await _refresh_one_loot_post(
                    bot, session, event, row)
                if wrote:
                    session.commit()
            except Exception as exc:  # noqa: BLE001 — isolate per row
                session.rollback()
                # A raise from in there is a spent Discord round trip in all but
                # the rarest cases (Forbidden on send/edit, 5xx, connection
                # reset), so charge it — an uncharged failure is what turns the
                # 5/tick cap into LOOT_POST_SCAN_LIMIT requests per tick — and
                # stamp the row so it stops leading the queue.
                attempted = True
                _stamp_loot_backoff(session, row)
                print(f"[team-loot] post refresh failed "
                      f"(event {getattr(event, 'id', None)}, "
                      f"team {row.team_id}): {exc}")
            if attempted:
                attempts += 1
                if attempts >= write_budget:
                    # Out of Discord budget for this tick. Untouched rows keep
                    # their older stamp and lead the ordering next tick.
                    return
    finally:
        session.close()
