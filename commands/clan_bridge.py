"""
Clan Chat Bridge Commands Module

Lets a group admin choose which bots (or webhooks) may speak into the game
through the clan chat bridge. By default the bridge relays only people: every
bot and webhook message in the bridge channel is ignored. Each ID added here
is stored in the ``clan_chat_bridge_allowed_bots`` group config key (also
editable on the website) and is honoured in the bridge channel only.

  /clan-bridge allow-bot <bot>   pick a bot from the member list
  /clan-bridge remove-bot <id>   autocompletes from the current list
  /clan-bridge bots              show the list and the bridge status
  "Allow in clan bridge"         message right-click; the only way to pick a
                                 webhook, which isn't a member

Authorization matches the /settings panel: Discord ADMINISTRATOR, or the
group's ``authed_users`` list.

Classes:
    ClanBridgeCommands: Extension containing the /clan-bridge commands
"""

import asyncio

from interactions import (
    AutocompleteContext, ContextMenuContext, ContextType, Extension, OptionType, Permissions,
    SlashContext, message_context_menu, slash_command, slash_option,
)

from db.models import Group, Guild, Session, User
from services import clan_chat_bridge as ccb
from services.group_config_writer import (
    get_group_config_values, set_group_config, validate_updates,
)

from .utils import is_admin, is_user_authorized

_DESCRIPTION = "Clan chat bridge settings"
_DENIED = "Only server administrators and the group's authorized users can change this."
_NO_GROUP = "This server isn't linked to a DropTracker group."


def _group_id_for_guild(guild_id):
    if not guild_id:
        return None
    s = Session()
    try:
        row = s.query(Guild.group_id).filter(Guild.guild_id == str(guild_id)).first()
        return int(row[0]) if row and row[0] else None
    finally:
        s.close()


def _is_authed_user(discord_id, group_id) -> bool:
    s = Session()
    try:
        group = s.query(Group).filter(Group.group_id == group_id).first()
        uid = s.query(User.user_id).filter(User.discord_id == str(discord_id)).first()
        return bool(group and uid and is_user_authorized(int(uid[0]), group))
    finally:
        s.close()


def _bridge_state(group_id) -> dict:
    return get_group_config_values(group_id, [
        ccb.BRIDGE_ENABLED_KEY, ccb.BRIDGE_CHANNEL_KEY, ccb.CLAN_NAME_KEY,
        ccb.BRIDGE_ALLOWED_BOTS_KEY,
    ])


def _save_allowlist(group_id, value, actor_discord_id) -> None:
    stored = validate_updates({ccb.BRIDGE_ALLOWED_BOTS_KEY: value}) if value else {
        ccb.BRIDGE_ALLOWED_BOTS_KEY: ""}
    set_group_config(group_id, stored, actor_discord_id=actor_discord_id,
                     action="config.update.discord.clan_bridge")
    # The listener runs in this process: drop its 60s routing cache so the
    # change applies to the very next message.
    ccb.invalidate_channel_map()


def _bridge_note(state: dict) -> str:
    """Where allowed bots will be heard, or what is still missing."""
    enabled = str(state.get(ccb.BRIDGE_ENABLED_KEY) or "").strip().lower() in ("1", "true")
    channel = str(state.get(ccb.BRIDGE_CHANNEL_KEY) or "").strip()
    if not str(state.get(ccb.CLAN_NAME_KEY) or "").strip():
        return "The bridge isn't running yet: set your clan chat name in the group settings."
    if not enabled or channel in ("", "0"):
        return "The bridge isn't running yet: turn it on and pick a bridge channel in the group settings."
    return f"Allowed bots are relayed from <#{channel}> only."


class ClanBridgeCommands(Extension):
    """/clan-bridge allow-bot | remove-bot | bots, plus a message context menu."""

    def __init__(self, bot):
        self.bot = bot

    def _own_ids(self) -> set:
        return {str(i) for i in (getattr(self.bot.user, "id", None),
                                 getattr(getattr(self.bot, "app", None), "id", None)) if i}

    async def _resolve(self, ctx):
        """(group_id, error message). Checks the link and the caller's rights."""
        group_id = await asyncio.to_thread(_group_id_for_guild, ctx.guild_id)
        if group_id is None:
            return None, _NO_GROUP
        if not await is_admin(ctx) and not await asyncio.to_thread(
                _is_authed_user, ctx.author.id, group_id):
            return None, _DENIED
        return group_id, None

    async def _allow(self, ctx, source_id: str, label: str):
        group_id, error = await self._resolve(ctx)
        if error:
            return await ctx.send(error, ephemeral=True)
        if source_id in self._own_ids():
            return await ctx.send("DropTracker's own messages are never relayed back into the game.",
                                  ephemeral=True)
        state = await asyncio.to_thread(_bridge_state, group_id)
        new_value, status = ccb.add_allowed_bot(state.get(ccb.BRIDGE_ALLOWED_BOTS_KEY), source_id)
        if status == "full":
            return await ctx.send(f"The list is full ({ccb.MAX_ALLOWED_BOTS} bots). "
                                  "Remove one with `/clan-bridge remove-bot` first.", ephemeral=True)
        if status == "invalid":
            return await ctx.send("That doesn't look like a Discord ID.", ephemeral=True)
        if status == "added":
            await asyncio.to_thread(_save_allowlist, group_id, new_value, ctx.author.id)
            head = f"{label} can now speak to your clan in game."
        else:
            head = f"{label} is already allowed."
        await ctx.send(f"{head}\n{_bridge_note(state)}", ephemeral=True)

    @slash_command(
        name="clan-bridge",
        description=_DESCRIPTION,
        sub_cmd_name="allow-bot",
        sub_cmd_description="Let a bot's messages in the bridge channel reach your clan in game",
        contexts=[ContextType.GUILD],
    )
    @slash_option(name="bot", description="The bot to allow",
                  opt_type=OptionType.USER, required=True)
    async def clan_bridge_allow_bot(self, ctx: SlashContext, bot):
        if not getattr(bot, "bot", False):
            return await ctx.send("That's a person, not a bot. People in the bridge channel "
                                  "are always relayed.", ephemeral=True)
        name = getattr(bot, "display_name", None) or getattr(bot, "username", "That bot")
        await self._allow(ctx, str(bot.id), f"**{name}**")

    @message_context_menu(
        name="Allow in clan bridge",
        default_member_permissions=Permissions.MANAGE_GUILD,
        contexts=[ContextType.GUILD],
    )
    async def clan_bridge_allow_from_message(self, ctx: ContextMenuContext):
        message = ctx.target
        author = getattr(message, "author", None)
        webhook_id = getattr(message, "webhook_id", None)
        if not webhook_id and not getattr(author, "bot", False):
            return await ctx.send("That message was written by a person. People in the bridge "
                                  "channel are always relayed.", ephemeral=True)
        name = getattr(author, "display_name", None) or getattr(author, "username", "that sender")
        if webhook_id:
            await self._allow(ctx, str(webhook_id), f"The **{name}** webhook")
        else:
            await self._allow(ctx, str(author.id), f"**{name}**")

    @slash_command(
        name="clan-bridge",
        description=_DESCRIPTION,
        sub_cmd_name="remove-bot",
        sub_cmd_description="Stop relaying a bot or webhook into the game",
        contexts=[ContextType.GUILD],
    )
    @slash_option(name="id", description="The bot or webhook to remove",
                  opt_type=OptionType.STRING, required=True, autocomplete=True)
    async def clan_bridge_remove_bot(self, ctx: SlashContext, id: str):
        group_id, error = await self._resolve(ctx)
        if error:
            return await ctx.send(error, ephemeral=True)
        target = str(id or "").strip().strip("<@!&>")
        state = await asyncio.to_thread(_bridge_state, group_id)
        new_value, removed = ccb.remove_allowed_bot(state.get(ccb.BRIDGE_ALLOWED_BOTS_KEY), target)
        if not removed:
            return await ctx.send(f"`{target}` isn't on the list.", ephemeral=True)
        await asyncio.to_thread(_save_allowlist, group_id, new_value, ctx.author.id)
        await ctx.send(f"Removed. {await self._label(target)} is no longer relayed into the game.",
                       ephemeral=True)

    @clan_bridge_remove_bot.autocomplete("id")
    async def clan_bridge_remove_bot_autocomplete(self, ctx: AutocompleteContext):
        group_id = await asyncio.to_thread(_group_id_for_guild, ctx.guild_id)
        if group_id is None:
            return await ctx.send(choices=[])
        state = await asyncio.to_thread(_bridge_state, group_id)
        typed = str(ctx.input_text or "").lower()
        choices = []
        for source_id in ccb.allowed_bot_list(state.get(ccb.BRIDGE_ALLOWED_BOTS_KEY)):
            user = self.bot.get_user(int(source_id))
            name = f"{user.username} ({source_id})" if user else source_id
            if typed in name.lower():
                choices.append({"name": name[:100], "value": source_id})
        await ctx.send(choices=choices[:25])

    @slash_command(
        name="clan-bridge",
        description=_DESCRIPTION,
        sub_cmd_name="bots",
        sub_cmd_description="Which bots can speak to your clan in game",
        contexts=[ContextType.GUILD],
    )
    async def clan_bridge_bots(self, ctx: SlashContext):
        group_id, error = await self._resolve(ctx)
        if error:
            return await ctx.send(error, ephemeral=True)
        state = await asyncio.to_thread(_bridge_state, group_id)
        ids = ccb.allowed_bot_list(state.get(ccb.BRIDGE_ALLOWED_BOTS_KEY))
        note = _bridge_note(state)
        if not ids:
            return await ctx.send("No bots are allowed, so only people are relayed into the game. "
                                  "Add one with `/clan-bridge allow-bot`, or right-click a bot's "
                                  f"message and pick **Apps > Allow in clan bridge**.\n{note}",
                                  ephemeral=True)
        lines = [f"- {await self._label(i)}" for i in ids]
        await ctx.send(f"**Bots allowed in the clan bridge ({len(ids)}/{ccb.MAX_ALLOWED_BOTS})**\n"
                       + "\n".join(lines) + f"\n{note}", ephemeral=True)

    async def _label(self, source_id: str) -> str:
        """A readable name for an allowlist entry. Webhooks aren't users, so
        a failed user lookup means a webhook (or a bot that has left)."""
        try:
            user = await self.bot.fetch_user(int(source_id))
        except Exception:
            user = None
        if user is not None:
            return f"**{user.username}** (`{source_id}`)"
        return f"webhook `{source_id}`"
