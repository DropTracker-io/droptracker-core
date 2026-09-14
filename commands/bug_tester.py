"""
Bug Tester Commands Module

``/bug-tester add | remove | list`` gives or withdraws the global Bug Tester
badge and its Discord role.

Restricted twice over: the command is registered only in the main DropTracker
server (``scopes``) and hidden from anyone without Administrator there, and
each handler also refuses anyone who isn't a bot owner before it reads or
writes anything.

The badge is the source of truth. It grants the complimentary supporter perks
(``db/entitlements.py``), and the role sync (``services/discord_roles.py``)
keeps the role on exactly the people who hold it. These handlers also change
the role immediately, so the result shows without waiting for the next pass.

Classes:
    BugTesterCommands: Extension containing the /bug-tester commands
"""

import asyncio
from typing import Optional

from interactions import (
    Extension, OptionType, Permissions, SlashContext, is_owner, slash_command, slash_option,
)

from db.app_logger import AppLogger
from services import discord_roles

app_logger = AppLogger()

MAIN_GUILD_ID = int(discord_roles.MAIN_GUILD_ID)
_DESCRIPTION = "Manage the global Bug Tester badge and role"
_DENIED = "Only bot owners can use this command, and only in the DropTracker server."
_CLAIM_HINT = "They need to link an in-game name with `/claim-rsn` first."


def _actor_user_id(session, discord_id) -> Optional[int]:
    from db.models import User

    row = session.query(User.user_id).filter(User.discord_id == str(discord_id)).first()
    return int(row[0]) if row is not None else None


def _grant(author_id, target_id, note: Optional[str]):
    from db.models import Session

    with Session() as session:
        actor = _actor_user_id(session, author_id)
        if actor is None:
            return discord_roles.BugTesterChange("no_actor")
        change = discord_roles.grant_bug_tester(session, str(target_id), actor, note=note)
        if change.status == "granted":
            session.commit()
        return change


def _revoke(author_id, target_id):
    from db.models import Session

    with Session() as session:
        actor = _actor_user_id(session, author_id)
        if actor is None:
            return discord_roles.BugTesterChange("no_actor")
        change = discord_roles.revoke_bug_tester(session, str(target_id), actor)
        if change.status == "revoked":
            session.commit()
        return change


def _holders():
    from db.models import Badge, Player, PlayerBadge, Session, User

    with Session() as session:
        return (
            session.query(User.discord_id, Player.player_name)
            .join(Player, Player.user_id == User.user_id)
            .join(PlayerBadge, PlayerBadge.player_id == Player.player_id)
            .join(Badge, Badge.badge_id == PlayerBadge.badge_id)
            .filter(Badge.key == discord_roles.BUG_TESTER_BADGE_KEY, PlayerBadge.status == "active")
            .order_by(Player.player_name)
            .all()
        )


class BugTesterCommands(Extension):
    """Owner-only commands for the global Bug Tester badge and role."""

    def __init__(self, bot):
        self.bot = bot

    async def _allowed(self, ctx: SlashContext) -> bool:
        if str(ctx.guild_id) != discord_roles.MAIN_GUILD_ID:
            return False
        return bool(await is_owner()(ctx))

    async def _set_role(self, discord_id, add: bool) -> Optional[str]:
        """Add or remove the role now. Returns a note for the reply when it didn't happen."""
        role_id = discord_roles.load_role_map().get("bug_tester")
        if not role_id:
            return "The role map isn't seeded yet, so the Discord role wasn't changed."
        try:
            if add:
                await self.bot.http.add_guild_member_role(
                    MAIN_GUILD_ID, discord_id, role_id, reason="Bug Tester granted by a bot owner")
            else:
                await self.bot.http.remove_guild_member_role(
                    MAIN_GUILD_ID, discord_id, role_id, reason="Bug Tester withdrawn by a bot owner")
            return None
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                return ("They aren't in this server; the role sync will give them the role when they join."
                        if add else "They aren't in this server.")
            if status == 403:
                return "I'm not allowed to change their roles here."
            app_logger.log(log_type="error", data=f"/bug-tester role change for {discord_id} failed: {exc}",
                           app_name="core", description="bug_tester")
            return "The Discord role change failed; the role sync will retry it."

    @slash_command(
        name="bug-tester",
        description=_DESCRIPTION,
        sub_cmd_name="add",
        sub_cmd_description="Give someone the Bug Tester badge and role",
        scopes=[MAIN_GUILD_ID],
        default_member_permissions=Permissions.ADMINISTRATOR,
    )
    @slash_option(name="user", description="Who to make a Bug Tester",
                  opt_type=OptionType.USER, required=True)
    @slash_option(name="note", description="Shown with the badge on their profile",
                  opt_type=OptionType.STRING, required=False, max_length=200)
    async def bug_tester_add(self, ctx: SlashContext, user, note: str = None):
        if not await self._allowed(ctx):
            return await ctx.send(_DENIED, ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            change = await asyncio.to_thread(_grant, ctx.author.id, user.id, (note or "").strip() or None)
        except Exception as exc:
            app_logger.log(log_type="error", data=f"/bug-tester add {user.id} failed: {exc}",
                           app_name="core", description="bug_tester")
            return await ctx.send("Couldn't update the badge. Check the logs.", ephemeral=True)

        who = f"<@{user.id}>"
        if change.status == "no_actor":
            return await ctx.send("Your Discord account isn't linked to a DropTracker user, "
                                  "so the award can't be attributed.", ephemeral=True)
        if change.status == "no_user":
            return await ctx.send(f"{who} has no DropTracker account. {_CLAIM_HINT}", ephemeral=True)
        if change.status == "no_players":
            return await ctx.send(f"{who} hasn't claimed an in-game name. {_CLAIM_HINT}", ephemeral=True)
        if change.status == "no_badge":
            return await ctx.send(f"The `{discord_roles.BUG_TESTER_BADGE_KEY}` badge is missing or inactive.",
                                  ephemeral=True)

        discord_roles.invalidate_user_perks(change.user_id)
        role_note = await self._set_role(user.id, add=True)
        await asyncio.to_thread(discord_roles.request_sync)
        accounts = ", ".join(f"**{name}**" for name in change.player_names)
        if change.status == "granted":
            message = (f"{who} is now a Bug Tester. The badge is on {accounts}, "
                       "and it comes with the supporter perks.")
        else:
            message = f"{who} already holds the Bug Tester badge (on {accounts})."
        await ctx.send(message + (f"\n{role_note}" if role_note else ""), ephemeral=True)

    @slash_command(
        name="bug-tester",
        description=_DESCRIPTION,
        sub_cmd_name="remove",
        sub_cmd_description="Withdraw someone's Bug Tester badge and role",
        scopes=[MAIN_GUILD_ID],
        default_member_permissions=Permissions.ADMINISTRATOR,
    )
    @slash_option(name="user", description="Who to remove as a Bug Tester",
                  opt_type=OptionType.USER, required=True)
    async def bug_tester_remove(self, ctx: SlashContext, user):
        if not await self._allowed(ctx):
            return await ctx.send(_DENIED, ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            change = await asyncio.to_thread(_revoke, ctx.author.id, user.id)
        except Exception as exc:
            app_logger.log(log_type="error", data=f"/bug-tester remove {user.id} failed: {exc}",
                           app_name="core", description="bug_tester")
            return await ctx.send("Couldn't update the badge. Check the logs.", ephemeral=True)
        if change.status == "no_actor":
            return await ctx.send("Your Discord account isn't linked to a DropTracker user, "
                                  "so the change can't be attributed.", ephemeral=True)

        who = f"<@{user.id}>"
        discord_roles.invalidate_user_perks(change.user_id)
        role_note = await self._set_role(user.id, add=False)
        await asyncio.to_thread(discord_roles.request_sync)
        if change.status == "revoked":
            accounts = ", ".join(f"**{name}**" for name in change.player_names)
            message = f"{who} is no longer a Bug Tester. The badge was revoked on {accounts}."
        else:
            message = f"{who} didn't hold the Bug Tester badge; any Bug Tester role they had is removed."
        await ctx.send(message + (f"\n{role_note}" if role_note else ""), ephemeral=True)

    @slash_command(
        name="bug-tester",
        description=_DESCRIPTION,
        sub_cmd_name="list",
        sub_cmd_description="Everyone who holds the Bug Tester badge",
        scopes=[MAIN_GUILD_ID],
        default_member_permissions=Permissions.ADMINISTRATOR,
    )
    async def bug_tester_list(self, ctx: SlashContext):
        if not await self._allowed(ctx):
            return await ctx.send(_DENIED, ephemeral=True)
        await ctx.defer(ephemeral=True)
        try:
            rows = await asyncio.to_thread(_holders)
        except Exception as exc:
            app_logger.log(log_type="error", data=f"/bug-tester list failed: {exc}",
                           app_name="core", description="bug_tester")
            return await ctx.send("Couldn't read the badge holders. Check the logs.", ephemeral=True)
        if not rows:
            return await ctx.send("Nobody holds the Bug Tester badge.", ephemeral=True)
        lines = [f"<@{discord_id}> — {name}" if discord_id else f"(no Discord) — {name}"
                 for discord_id, name in rows]
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900].rsplit("\n", 1)[0] + "\n…"
        await ctx.send(f"**Bug Testers ({len(rows)})**\n{body}", ephemeral=True)
