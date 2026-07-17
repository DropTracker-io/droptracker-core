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
MEMBER_OPS_LIMIT = 40


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


async def _ensure_channel(bot, guild, row, team, config) -> None:
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
            post = await forum.create_post(
                name,
                f"Team thread for **{team.name}** — task progress, lead "
                f"changes and roll prompts land here.",
            )
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
    role = None
    if row.role_id:
        try:
            role = await guild.fetch_role(int(row.role_id))
        except Exception:
            role = None

    for uid in to_add:
        if ops >= MEMBER_OPS_LIMIT:
            break
        applied = False
        if role is not None:
            try:
                member = await guild.fetch_member(int(uid))
                if member is not None:
                    await member.add_role(role, reason=PROVISION_REASON)
                    applied = True
            except Exception:
                pass
        if thread is not None:
            try:
                await thread.add_member(int(uid))
                applied = True
            except Exception:
                pass
        # Not in the guild (or both surfaces failed): count as handled so one
        # unlinked/absent player can't wedge the diff forever. They get picked
        # up on the next members_dirty pass after they join.
        state.add(uid)
        ops += 1 if applied else 0

    for uid in to_remove:
        if ops >= MEMBER_OPS_LIMIT:
            break
        if role is not None:
            try:
                member = await guild.fetch_member(int(uid))
                if member is not None:
                    await member.remove_role(role, reason=PROVISION_REASON)
            except Exception:
                pass
        if thread is not None:
            try:
                await thread.remove_member(int(uid))
            except Exception:
                pass
        state.discard(uid)
        ops += 1

    row.member_state = json.dumps(sorted(state))
    return state == desired


async def reconcile_event_team_discord_once(bot, session_factory, redis_client) -> None:
    """One reconcile pass (called from the bots/main.py interval task)."""
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
                        await _ensure_channel(bot, guild, row, team, scope["config"])
                    elif row.channel_id:
                        await _delete_discord_objects(bot, None, None, row.channel_id)
                        row.channel_id = None
                        row.channel_kind = None
                    row.sync_status = "synced"
                    row.synced_at = now
                    row.last_error = None
                    row.members_dirty = True
                    session.commit()

                if row.members_dirty:
                    desired = _desired_member_ids(session, row.team_id)
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
