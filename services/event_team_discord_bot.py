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
row is marked ``failed`` with ``last_error`` and is NOT re-polled (no retry
storm on a permanent Forbidden) — the next config save / team edit flips it
back to ``pending``. Sessions are opened per tick and always closed
(rollback-on-error) — the idle-transaction lessons apply here too.
"""
from __future__ import annotations

import asyncio
import json
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
            bot, data.get("guild_id"), data.get("role_id"), data.get("channel_id"))


async def _delete_discord_objects(bot, guild_id, role_id, channel_id) -> None:
    """Best-effort teardown of one row's channel + role. Missing objects (bot
    kicked, already deleted by hand) just mean nothing left to clean up."""
    if channel_id:
        try:
            channel = await bot.fetch_channel(int(channel_id))
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
    if color is not None and int(getattr(role, "color", 0) or 0) != color:
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


async def _ensure_channel(bot, guild, row, team, config, event) -> None:
    """Create/rename the team channel: a thread in the configured forum, or a
    private text channel visible to the team role (public when roles are off).
    Writes row.channel_id / row.channel_kind back."""
    import interactions
    from services.event_team_discord import channel_name_for_team

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
        name = team.name[:100]
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

    name = channel_name_for_team(team.name)
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
            permission_overwrites=overwrites or interactions.MISSING,
            reason=PROVISION_REASON,
        )
        row.channel_id = str(channel.id)
        row.channel_kind = "text"
    elif getattr(channel, "name", None) != name:
        await channel.edit(name=name, reason=PROVISION_REASON)


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
        """One member operation -> 'ok' | 'absent' | 'error'. Absent-from-guild
        (404) and permission-refused (403) are EXPECTED for rosters that
        include people outside the server — swallowed without a log so they
        never reach Sentry, and marked handled. Anything ELSE (network blip,
        5xx, gateway trouble) is transient: the id must NOT be marked handled,
        or a member silently loses their role forever — exactly what happened
        when the first sync ran during the 2026-07-17 pool outage."""
        from interactions.client import errors as ix_errors

        try:
            await coro
            return "ok"
        except (ix_errors.NotFound, ix_errors.Forbidden):
            return "absent"
        except Exception:
            return "error"

    guild_id = int(row.guild_id)
    role_id = int(row.role_id) if row.role_id else None
    had_errors = False

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
        if "error" in results:
            had_errors = True
            continue  # keep in state; retried next pass
        state.discard(uid)

    row.member_state = json.dumps(sorted(state))
    return state == desired and not had_errors


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
    from services.event_team_discord import team_discord_scopes, team_flags

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
        # Scope configs are per event — cache across rows of the same event.
        scopes_cache: dict = {}
        for row in rows:
            try:
                if row.sync_status == "delete_pending":
                    if row.delete_after and now < row.delete_after:
                        continue  # 48h grace still running
                    await _delete_discord_objects(
                        bot, row.guild_id, row.role_id, row.channel_id)
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
                        await _ensure_channel(bot, guild, row, team,
                                              scope["config"], event)
                    elif row.channel_id:
                        await _delete_discord_objects(bot, None, None, row.channel_id)
                        row.channel_id = None
                        row.channel_kind = None
                    row.sync_status = "synced"
                    row.synced_at = now
                    row.last_error = None
                    row.members_dirty = True
                    session.commit()
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
            except Exception as exc:  # noqa: BLE001 — isolate per row
                session.rollback()
                try:
                    row.sync_status = "failed"
                    row.last_error = str(exc)[:255]
                    session.commit()
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

    message = None
    if row.board_message_id:
        try:
            message = await channel.fetch_message(message_id=row.board_message_id)
        except Exception:
            message = None  # deleted / inaccessible — repost below
    if message is not None:
        # attachments=[] drops the previous upload so files don't accumulate.
        if board_file is not None:
            await message.edit(components=components, files=board_file,
                               attachments=[])
        else:
            await message.edit(components=components, attachments=[])
    else:
        if board_file is not None:
            message = await channel.send(components=components, files=board_file)
        else:
            message = await channel.send(components=components)
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


async def _board_post_pass(bot, session_factory, redis_client,
                           render_budget: int) -> None:
    from db.models import Event, EventTeamDiscord
    from services.event_team_discord import TEAM_BOARD_DIRTY_KEY

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
                           .filter(EventTeamDiscord.sync_status == "synced",
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
                            EventTeamDiscord.sync_status == "synced",
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
