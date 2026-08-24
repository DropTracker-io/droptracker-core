"""
User Commands Module

Contains all user-level Discord slash commands that don't require special permissions.
These commands are available to all users and handle personal settings, account management,
and basic bot interactions.

Classes:
    UserCommands: Extension containing user-level slash commands

Author: joelhalen
"""

import json
from datetime import datetime
from secrets import token_hex
from data.submissions import try_create_player
from interactions import AutocompleteContext, SlashContext, Embed, OptionType, Extension, slash_command, slash_option
from db.models import Session, User, Group, Guild, Player, UserConfiguration, session, PlayerPoints
from db.player_claims import claim_player, unclaim_player
from services.components import build_help_components
from services.points import award_points_to_player
from utils.format import format_time_since_update, get_command_id, get_player_by_claim_rsn
from utils.site_urls import player_url
from utils.app_emojis import emoji as app_emoji
from utils.wiseoldman import check_user_by_username
from .utils import try_create_user, is_admin, is_user_authorized
from sqlalchemy import func


def _time_since_iso(claimed_at):
    """ISO timestamp (service result) -> the command's relative-time phrase."""
    if not claimed_at:
        return ""
    try:
        return format_time_since_update(datetime.fromisoformat(claimed_at))
    except Exception:
        return ""


class UserCommands(Extension):
    """
    Extension containing user-level Discord slash commands.
    
    This extension provides commands that regular users can execute to manage
    their accounts, configure settings, and interact with the DropTracker system.
    """
    
    def __init__(self, bot):
        """
        Initialize the UserCommands extension.
        
        Args:
            bot: The Discord bot instance
        """
        self.bot = bot
        self.message_handler = bot.get_ext("services.message_handler")

    def _refresh_session(self):
        """
        Reset scoped session state before handling a new interaction.

        This prevents long-lived transaction snapshots from returning stale
        reads when underlying data was changed by another process.
        """
        session.remove()


    def _get_group_for_guild(self, guild_id):
        if not guild_id:
            return None
        guild = session.query(Guild).filter(Guild.guild_id == str(guild_id)).first()
        if not guild or not guild.group_id:
            return None
        return session.query(Group).filter(Group.group_id == guild.group_id).first()


    @slash_command(name="help",
                   description="View helpful commands/links for the DropTracker")
    async def help(self, ctx: SlashContext):
        """
        Display help information and useful links for the DropTracker.
        
        Shows a comprehensive help interface with buttons and links to
        various DropTracker resources and commands.
        
        Args:
            ctx (SlashContext): The slash command context
        """
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=ctx.user.id).first()
        if not user:
            await try_create_user(ctx=ctx)
        user = session.query(User).filter(User.discord_id == ctx.author.id).first()
        return await ctx.send(components=await build_help_components(self.bot), ephemeral=True)

    @slash_command(name="dm-settings",
                   description="View or change your direct message settings")
    @slash_option(name="dm_type",
                  description="Select which type of direct message setting you want to edit",
                  required=True,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    @slash_option(name="toggle",
                  description="Select whether you want to enable or disable the direct message setting",
                  required=True,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    async def dm_settings_cmd(self, ctx: SlashContext, dm_type: str, toggle: str):
        """
        Configure direct message notification settings.

        Only settings the backend actually acts on are offered. Legacy
        "updates"/"points" toggles were removed 2026-07-07 — nothing ever sent
        those DMs (user_configurations rows they wrote were never read).

        Args:
            ctx (SlashContext): The slash command context
            dm_type (str): Type of DM setting ("account_changes")
            toggle (str): Whether to "enable" or "disable" the setting
        """
        self._refresh_session()

        def set_dm_config(user, config_keys, value):
            """Helper to set one or more config values for a user."""
            for config_key in config_keys:
                config_entry = session.query(UserConfiguration).filter(
                    UserConfiguration.user_id == user.user_id,
                    UserConfiguration.config_key == config_key
                ).first()
                if config_entry:
                    config_entry.config_value = value
                else:
                    # If config entry doesn't exist, create it
                    config_entry = UserConfiguration(
                        user_id=user.user_id,
                        config_key=config_key,
                        config_value=value
                    )
                    session.add(config_entry)

        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == str(ctx.user.id)).first()

        # Determine which config keys to update
        if dm_type == "account_changes":
            config_keys = ["dm_account_changes"]
            desc_ext = "- Account name changes"
        else:
            embed = Embed(
                title="Unknown setting",
                description="Pick a setting from the list. (You can also manage this at https://www.droptracker.io/settings)"
            )
            await ctx.send(embed=embed, ephemeral=True)
            return

        value = "true" if toggle == "enable" else "false"
        set_dm_config(user, config_keys, value)
        session.commit()

        if toggle == "enable":
            embed = Embed(
                title="Success!",
                description=f"You have enabled direct-message notifications from me for:\\n" + desc_ext
            )
        else:
            embed = Embed(
                title="Success!",
                description=f"You have disabled direct-message notifications from me for:\\n" + desc_ext
            )
        await ctx.send(embed=embed, ephemeral=True)

    @dm_settings_cmd.autocomplete("dm_type")
    async def dm_settings_autocomplete_dm_type(self, ctx: AutocompleteContext):
        """Provide autocomplete options for DM settings type."""
        await ctx.send(
            choices=[
                {"name": "Account name changes", "value": "account_changes"},
            ]
        )

    @dm_settings_cmd.autocomplete("toggle")
    async def dm_settings_autocomplete_toggle(self, ctx: AutocompleteContext):
        """Provide autocomplete options for enable/disable toggle."""
        await ctx.send(
            choices=[
                {"name": "Enable", "value": "enable"},
                {"name": "Disable", "value": "disable"}
            ]
        )
    
    @slash_command(name="pingme",
                   description="Toggle whether or not you want to be pinged when your submissions are sent to Discord")
    @slash_option(name="type",
                  description="Select whether you want to toggle global, or clan-specific pings.",
                  required=True,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    async def pingme_cmd(self, ctx: SlashContext, type: str):
        """
        Configure ping settings for submission notifications.
        
        Allows users to control when they get pinged by the bot for their
        submissions in different contexts (global, group, or nowhere).
        
        Args:
            ctx (SlashContext): The slash command context
            type (str): Ping type ("global", "group", "everywhere")
        """
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
            
        if type == "global":
            user.global_ping = not user.global_ping
            session.commit()
            if user.global_ping:
                embed = Embed(title="Success!",
                              description=f"You will now be pinged when your submissions are sent to Discord.")
                await ctx.send(embed=embed, ephemeral=True)
            else:
                embed = Embed(title="Success!",
                              description=f"You will **no longer** be pinged when your submissions are sent to Discord.")
                await ctx.send(embed=embed, ephemeral=True)
        elif type == "group":
            user.group_ping = not user.group_ping
            session.commit()
            if user.group_ping:
                embed = Embed(title="Success!",
                              description=f"You will now be pinged when your submissions are sent to Discord.")
                await ctx.send(embed=embed, ephemeral=True)
            else:
                embed = Embed(title="Success!",
                              description=f"You will **no longer** be pinged when your submissions are sent to Discord.")
                await ctx.send(embed=embed, ephemeral=True)
        elif type == "everywhere":
            user.never_ping = not user.never_ping
            session.commit()
            if user.never_ping:
                embed = Embed(title="Success!",
                              description=f"You will **no longer** be pinged `anywhere` when your submissions are sent to Discord.")
                await ctx.send(embed=embed, ephemeral=True)
            else:
                embed = Embed(title="Success!",
                              description=f"You **will now be pinged** `anywhere` when your submissions are sent to Discord.")
                await ctx.send(embed=embed, ephemeral=True)

    @pingme_cmd.autocomplete("type")
    async def pingme_autocomplete_type(self, ctx: AutocompleteContext):
        """Provide autocomplete options for ping types."""
        await ctx.send(
            choices=[
                {"name": f"Globally", "value": "global"},
                {"name": f"In my group", "value": "group"},
                {"name": f"Everywhere", "value": "everywhere"}
            ]
        )
    
    @slash_command(name="hideme",
                   description="Toggle whether or not you will appear anywhere in the global discord server / side panel / etc.")
    @slash_option(name="account",
                  description="Select which of your accounts you want to hide from our global listings (all for all).",
                  required=True,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    async def hideme_cmd(self, ctx: SlashContext, account: str):
        """
        Configure visibility settings for accounts in public listings.
        
        Allows users to hide their accounts from global leaderboards and
        public displays while still participating in group activities.
        
        Args:
            ctx (SlashContext): The slash command context
            account (str): Account name to hide, or "all" for all accounts
        """
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
            
        if account == "all":
            user.hidden = not user.hidden
            session.commit()
            if user.hidden:
                embed = Embed(title="Success!", 
                              description=f"All of your accounts will **no longer** be visible in our global listings.\nYou can also manage this from the website account settings page.")
                return await ctx.send(embed=embed, ephemeral=True)
            else:
                embed = Embed(title="Success!",
                              description=f"All of your accounts will now **be visible** in our global listings.\nYou can also manage this from the website account settings page.")
                return await ctx.send(embed=embed, ephemeral=True)
        else:
            player = session.query(Player).filter_by(player_name=account).first()
            if not player:
                return await ctx.send(f"You don't have any accounts by that name.", ephemeral=True)
            player.hidden = not player.hidden
            session.commit()
            if player.hidden:
                embed = Embed(title="Success!",
                              description=f"Your account, `{player.player_name}` will **no longer** be visible in our global listings.\nYou can also change this from your website account page.")
                return await ctx.send(embed=embed, ephemeral=True)
            else:
                embed = Embed(title="Success!",
                              description=f"Your account, `{player.player_name}` will now **be visible** in our global listings.\nYou can also change this from your website account page.")
                return await ctx.send(embed=embed, ephemeral=True)

    @hideme_cmd.autocomplete("account")
    async def hideme_autocomplete_account(self, ctx: AutocompleteContext):
        """Provide autocomplete options for user accounts."""
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        
        if not user:
            # User not found in database
            return await ctx.send(
                choices=[{"name": "All accounts", "value": "all"}]
            )
        
        # Query for the user's accounts
        accounts = session.query(Player).filter_by(user_id=user.user_id).all()
        
        # Always include "All accounts" option
        choices = [{"name": "All accounts", "value": "all"}]
        
        # Add player accounts if they exist
        if accounts:
            choices.extend([
                {"name": account.player_name, "value": account.player_name}
                for account in accounts
            ])
        
        return await ctx.send(choices=choices)
            
    @slash_command(name="accounts",
                   description="View your currently claimed RuneScape character names, if you have any")
    async def user_accounts_cmd(self, ctx: SlashContext):
        """
        Display all accounts claimed by the user.
        
        Shows a list of all OSRS accounts associated with the user's Discord account,
        including their IDs and last update times.
        
        Args:
            ctx (SlashContext): The slash command context
        """
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
            
        accounts = session.query(Player).filter_by(user_id=user.user_id)
        account_names = ""
        count = 0
        if accounts:
            for account in accounts:
                count += 1
                last_updated_unix = format_time_since_update(account.date_updated)
                account_names += f"`" + account.player_name.strip() + f"` (id: {account.player_id})\\n> Last updated: {last_updated_unix}\\n"
                
        account_emb = Embed(title="Your Registered Accounts:",
                            description=f"{account_names}(total: `{count}`)")
        claim_rsn_id = await get_command_id(self.bot, "claim-rsn")
        if claim_rsn_id and claim_rsn_id != "`command not yet added`":
            claim_rsn_mention = f"</claim-rsn:{claim_rsn_id}>"
        else:
            claim_rsn_mention = "`/claim-rsn`"
        account_emb.add_field(name="/claim-rsn",value=f"To claim another, you can use the {claim_rsn_mention} command.", inline=False)
        account_emb.add_field(name="/unclaim-rsn", value="To remove an account, use the `/unclaim-rsn` command.", inline=False)
        account_emb.set_footer(text="https://www.droptracker.io/")
        await ctx.send(embed=account_emb, ephemeral=True)
    
    @slash_command(name="claim-rsn",
                    description="Claim ownership of your RuneScape account names in the DropTracker database")
    @slash_option(name="rsn",
                  opt_type=OptionType.STRING,
                  description="Please type the in-game-name of the account you want to claim, **exactly as it appears**!",
                  required=True)
    async def claim_rsn_command(self, ctx: SlashContext, rsn: str):
        """
        Claim ownership of a RuneScape account.
        
        Associates an OSRS account with the user's Discord account, allowing
        them to receive notifications and participate in group activities.
        
        Args:
            ctx (SlashContext): The slash command context
            rsn (str): The RuneScape username to claim
        """
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            # Bot-side creation keeps the Discord "registered" role + DM flow;
            # the shared service would only do the DB portion.
            await try_create_user(ctx=ctx)

        # Shared mutation logic (also behind the website/Activity claim flow).
        result = claim_player(
            rsn,
            discord_id=str(ctx.user.id),
            username=ctx.user.username,
            guild_id=ctx.guild.id if ctx.guild else None,
        )
        status = result["status"]

        if status == "not_found":
            embed = Embed(title="Player not found!",
                          description=f"`{rsn}` was not yet found in our database.\n" +
                          f"A player must have first used our plugin to be claimed on Discord.\n" +
                          "To fix:\n" +
                          "1. Install the [DropTracker Plugin](https://www.droptracker.io/runelite) on RuneLite\n" +
                          "2. Receive some loot, or another achievement in-game with the plugin enabled\n" +
                          f"3. Then, try using </claim-rsn:{await get_command_id(self.bot, 'claim-rsn')}> again!\n\n" +
                          "-# P.S.: It is highly recommended that you [enable API connections](https://www.droptracker.io/wiki/why-api) in our plugin's " +
                          "configuration for the most robust tracking accuracy!")
            return await ctx.send(embeds=embed,ephemeral=True)
        joined_time = _time_since_iso(result.get("claimed_at"))
        if status == "claimed_by_other":
            owner_discord_id = result.get("owner_discord_id")
            await ctx.send(f"Uh-oh!\\n" +
                        f"It looks like somebody else may have claimed your account {joined_time}!\\n" +
                        f"<@{owner_discord_id}> (discord id: {owner_discord_id}) currently owns it in our database.\\n" +
                        "If this is some type of mistake, please reach out in our discord server:\\n" +
                        "https://discord.gg/droptracker",
                        ephemeral=True)
        elif status == "already_yours":
            await ctx.send(f"It looks like you've already claimed this account ({result.get('player_name')}) {joined_time}\\n" +
                        "\\nSomething not seem right?\\n" +
                        "Please reach out in our discord server:\\n" +
                        "https://discord.gg/droptracker",
                        ephemeral=True)
        elif status == "claimed":
            embed = Embed(title="Success!",
                          description=f"Your in-game name has been successfully associated with your Discord account.\n" +
                          "That's it!")
            if result.get("group_id") and result["group_id"] != 2:
                embed.add_field(name="Group", value=f"You've been added to **{result.get('group_name')}**.", inline=False)
            embed.add_field(name=f"What's next?",value=f"**Configure your account settings**\n" + 
                            "You can visit [our website](https://www.droptracker.io/), and sign-in with your Discord account to access privacy and visibility settings for your characters.\n" + 
			                f"**Do you have {app_emoji('construction')} 83 Construction?**\n" +
                            "> You can visit your Adventure Log (Achievement Gallery room of your own POH) to load all of your existing personal bests into our database instantly.\n" + 
			                "**Open your Collection Log**\n" + 
			                f"> Upload all (un)locked Collection Log slots instantly--just open the interface with our plugin enabled, then visit [your profile]({player_url(result.get('player_id'))})!", inline=False)
            embed.set_thumbnail(url="https://www.droptracker.io/img/droptracker-small.gif")
            embed.set_footer(text="Powered by the DropTracker | https://www.droptracker.io/")
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"An error occurred claiming your account.\n" +
                           "Try again later, or reach out in our Discord server",
                           ephemeral=True)
    
    @slash_command(name="unclaim-rsn",
                    description="Remove a RuneScape account name from your Discord account")
    @slash_option(name="rsn",
                  opt_type=OptionType.STRING,
                  description="The in-game name of the account you want to unclaim",
                  required=True,
                  autocomplete=True)
    async def unclaim_rsn_command(self, ctx: SlashContext, rsn: str):
        """
        Unclaim a RuneScape account from the user's Discord account.

        Removes the association between an OSRS account and the user's Discord
        account, and removes the player from any groups they were added to via
        claiming.

        Args:
            ctx (SlashContext): The slash command context
            rsn (str): The RuneScape username to unclaim
        """
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()

        if not user:
            return await ctx.send("You don't have any accounts associated with your Discord account.",
                                  ephemeral=True)

        # Shared mutation logic (also behind the website unclaim endpoint).
        result = unclaim_player(discord_id=str(ctx.user.id), rsn=rsn)
        status = result["status"]

        if status == "not_found":
            return await ctx.send(
                f"`{rsn}` was not found in our database.",
                ephemeral=True
            )

        if status == "not_yours":
            return await ctx.send(
                f"`{result.get('player_name')}` is not currently claimed by your Discord account.",
                ephemeral=True
            )

        if status != "unclaimed":
            return await ctx.send(
                "An error occurred while unclaiming your account. Please try again later.",
                ephemeral=True
            )

        embed = Embed(
            title="Account unclaimed",
            description=f"`{result.get('player_name')}` has been removed from your Discord account.\n"
                        "You can re-claim it at any time using "
                        f"</claim-rsn:{await get_command_id(self.bot, 'claim-rsn')}>."
        )
        embed.set_footer(text="Powered by the DropTracker | https://www.droptracker.io/")
        await ctx.send(embed=embed, ephemeral=True)

    @unclaim_rsn_command.autocomplete("rsn")
    async def unclaim_rsn_autocomplete(self, ctx: AutocompleteContext):
        """Provide autocomplete options from the user's currently claimed accounts."""
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()

        if not user:
            return await ctx.send(choices=[])

        accounts = session.query(Player).filter_by(user_id=user.user_id).all()

        if not accounts:
            return await ctx.send(choices=[])

        return await ctx.send(choices=[
            {"name": account.player_name, "value": account.player_name}
            for account in accounts
        ])

    @slash_command(
        name="my-points",
        description="View your earned points across your groups",
    )
    async def my_points_cmd(self, ctx: SlashContext):
        self._refresh_session()
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == str(ctx.user.id)).first()

        players = session.query(Player).filter(Player.user_id == user.user_id).all()
        if not players:
            return await ctx.send(
                "No claimed accounts were found for your user. Use `/claim-rsn` first.",
                ephemeral=True
            )

        player_ids = [p.player_id for p in players]
        player_name_by_id = {p.player_id: p.player_name for p in players}

        current_group = self._get_group_for_guild(ctx.guild_id)
        current_group_total = 0
        if current_group:
            current_group_total = int(
                session.query(func.coalesce(func.sum(PlayerPoints.amount), 0))
                .filter(
                    PlayerPoints.group_id == current_group.group_id,
                    PlayerPoints.player_id.in_(player_ids),
                )
                .scalar() or 0
            )

        per_group_rows = (
            session.query(
                PlayerPoints.group_id,
                func.coalesce(func.sum(PlayerPoints.amount), 0).label("total_points")
            )
            .filter(
                PlayerPoints.player_id.in_(player_ids),
                PlayerPoints.group_id.isnot(None),
            )
            .group_by(PlayerPoints.group_id)
            .order_by(func.sum(PlayerPoints.amount).desc())
            .all()
        )

        total_points = int(sum(int(r.total_points or 0) for r in per_group_rows))
        if total_points <= 0:
            return await ctx.send(
                "You have not earned any group points yet.",
                ephemeral=True
            )

        group_ids = [int(r.group_id) for r in per_group_rows if r.group_id is not None]
        groups = session.query(Group).filter(Group.group_id.in_(group_ids)).all() if group_ids else []
        group_name_by_id = {g.group_id: g.group_name for g in groups}

        per_player_rows = (
            session.query(
                PlayerPoints.player_id,
                func.coalesce(func.sum(PlayerPoints.amount), 0).label("total_points")
            )
            .filter(PlayerPoints.player_id.in_(player_ids))
            .group_by(PlayerPoints.player_id)
            .order_by(func.sum(PlayerPoints.amount).desc())
            .all()
        )

        embed = Embed(
            title="Your Group Points",
            description=f"Total points across all groups: **{total_points:,}**",
        )
        if current_group:
            embed.add_field(
                name=f"Current Server Group ({current_group.group_name})",
                value=f"**{current_group_total:,}** point(s)",
                inline=False
            )

        player_lines = []
        for row in per_player_rows[:10]:
            player_name = player_name_by_id.get(int(row.player_id), f"Player {row.player_id}")
            player_lines.append(f"`{player_name}` - **{int(row.total_points or 0):,}**")
        if player_lines:
            embed.add_field(name="Your Accounts", value="\n".join(player_lines), inline=False)

        group_lines = []
        for row in per_group_rows[:10]:
            gid = int(row.group_id)
            gname = group_name_by_id.get(gid, f"Group #{gid}")
            group_lines.append(f"`{gname}` - **{int(row.total_points or 0):,}**")
        if group_lines:
            embed.add_field(name="Top Groups For You", value="\n".join(group_lines), inline=False)

        embed.set_footer(text="Points are based on tracked group point awards.")
        await ctx.send(embed=embed, ephemeral=True)

    @slash_command(
        name="group-points",
        description="View this server group's point standings",
    )
    async def group_points_cmd(self, ctx: SlashContext):
        self._refresh_session()
        if not ctx.guild_id:
            return await ctx.send("Use this command inside your group's Discord server.", ephemeral=True)

        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send("This server is not linked to a DropTracker group.", ephemeral=True)

        total_points = int(
            session.query(func.coalesce(func.sum(PlayerPoints.amount), 0))
            .filter(PlayerPoints.group_id == group.group_id)
            .scalar() or 0
        )
        total_awards = int(
            session.query(func.count(PlayerPoints.id))
            .filter(PlayerPoints.group_id == group.group_id)
            .scalar() or 0
        )
        total_players = int(
            session.query(func.count(func.distinct(PlayerPoints.player_id)))
            .filter(PlayerPoints.group_id == group.group_id)
            .scalar() or 0
        )

        top_rows = (
            session.query(
                Player.player_name,
                func.coalesce(func.sum(PlayerPoints.amount), 0).label("total_points")
            )
            .join(PlayerPoints, PlayerPoints.player_id == Player.player_id)
            .filter(PlayerPoints.group_id == group.group_id)
            .group_by(Player.player_id, Player.player_name)
            .order_by(func.sum(PlayerPoints.amount).desc())
            .limit(10)
            .all()
        )

        embed = Embed(
            title=f"{group.group_name} - Group Points",
            description=f"**{total_points:,}** total point(s) across **{total_players:,}** player(s).",
        )
        embed.add_field(name="Award Entries", value=f"{total_awards:,}", inline=True)

        if top_rows:
            lines = [
                f"**{idx}.** `{row.player_name}` - **{int(row.total_points or 0):,}**"
                for idx, row in enumerate(top_rows, start=1)
            ]
            embed.add_field(name="Top Players", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Top Players", value="No points have been awarded yet.", inline=False)

        await ctx.send(embed=embed, ephemeral=True)

    @slash_command(
        name="sync-wom",
        description="Request an immediate WiseOldMan membership sync for this group (1-hour cooldown)",
    )
    async def sync_wom_cmd(self, ctx: SlashContext):
        """
        Trigger a WiseOldMan membership sync for the group linked to this Discord server.

        Available to guild administrators and users listed in the group's ``authed_users``
        configuration.  Enforces a 1-hour per-group cooldown to avoid hammering the WOM API.
        The response is deferred so the interaction won't time out during a long sync.
        """
        self._refresh_session()

        if not ctx.guild_id:
            return await ctx.send(
                "This command must be used inside your group's Discord server.",
                ephemeral=True,
            )

        guild = session.query(Guild).filter(Guild.guild_id == str(ctx.guild_id)).first()
        if not guild or not guild.group_id:
            return await ctx.send(
                "This Discord server is not linked to a DropTracker group.",
                ephemeral=True,
            )

        group = session.query(Group).filter(Group.group_id == guild.group_id).first()
        if not group or not group.wom_id:
            return await ctx.send(
                "The linked group has no WOM ID configured.",
                ephemeral=True,
            )

        # Authorization: guild administrator OR listed in the group's authed_users config
        user = session.query(User).filter(User.discord_id == str(ctx.author.id)).first()
        if not await is_admin(ctx) and not (user and is_user_authorized(user.user_id, group)):
            return await ctx.send(
                "You are not authorized to request a WOM sync for this group.",
                ephemeral=True,
            )

        await ctx.defer(ephemeral=True)

        try:
            from db.ops import sync_group_from_wom_with_stats
            result = await sync_group_from_wom_with_stats(wom_id=int(group.wom_id))
        except Exception as e:
            print(f"WOM sync failed for WOM group {group.wom_id}: {e}")
            return await ctx.send(
                "WOM sync failed — please try again in a few minutes.",
                ephemeral=True,
            )

        if result["on_cooldown"]:
            remaining = result["cooldown_remaining_seconds"]
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60
            time_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
            embed = Embed(
                title="Sync on Cooldown",
                description=(
                    f"**{group.group_name}** was synced recently.\n"
                    f"Please wait **{time_str}** before requesting another sync."
                ),
                color=0xE67E22,
            )
            return await ctx.send(embed=embed, ephemeral=True)

        added = result["added"]
        removed = result["removed"]

        def _player_list(names, max_shown=15):
            if not names:
                return "None"
            shown = names[:max_shown]
            extra = len(names) - max_shown
            text = ", ".join(f"`{n}`" for n in shown)
            if extra > 0:
                text += f" *(+{extra} more)*"
            return text

        embed = Embed(
            title="WOM Sync Complete",
            description=f"Membership sync with WiseOldMan finished for **{result['group_name']}**.",
            color=0x2ECC71,
        )
        embed.add_field(name="Total Members", value=str(result["total_members"]), inline=True)
        embed.add_field(name="Added", value=str(len(added)), inline=True)
        embed.add_field(name="Removed", value=str(len(removed)), inline=True)

        if added:
            embed.add_field(name="New Members", value=_player_list(added), inline=False)
        if removed:
            embed.add_field(name="Removed Members", value=_player_list(removed), inline=False)

        if result["skipped_removals"]:
            embed.add_field(
                name="Warning: Removal Pass Skipped",
                value=(
                    "WOM returned an incomplete member list — no members were removed "
                    "this cycle to prevent incorrectly evicting valid members."
                ),
                inline=False,
            )

        embed.set_footer(
            text=f"WOM ID: {result['wom_id']} • Duration: {result['duration_seconds']:.1f}s"
        )
        await ctx.send(embed=embed, ephemeral=True)
    @slash_command(name="clan-log",
                   description="See which boss uniques your clan has obtained, and what's still missing")
    @slash_option(name="period",
                  description="all (default), a year like 2026, or a month like 2026-08",
                  required=False,
                  opt_type=OptionType.STRING)
    async def clan_log_cmd(self, ctx: SlashContext, period: str = "all"):
        """Post the clan's unique-completion card.

        Public rather than ephemeral: a completion board is something a clan
        shows each other, and the reason the standing-message config exists at
        all is that people want it visible. The card is rendered from the
        stored board, so this costs a screenshot at worst and a Redis hit at
        best — never a recompute.
        """
        self._refresh_session()

        from interactions import ActionRow, Button, ButtonStyle
        from services.clan_log import is_valid_period
        from services.clan_log_discord import board_url, build_card_payload

        period = (period or "all").strip() or "all"
        if not is_valid_period(period):
            return await ctx.send(
                "That period doesn't look right — use `all`, a year like `2026`, "
                "or a month like `2026-08`.",
                ephemeral=True,
            )

        group = self._get_group_for_guild(ctx.guild_id)
        if not group:
            return await ctx.send(
                "This Discord server isn't linked to a DropTracker clan yet.",
                ephemeral=True,
            )

        # Rendering the card is a headless screenshot; Discord's 3s window is
        # not enough on a cold cache.
        await ctx.defer()

        text, file, _state_hash = await build_card_payload(session, group.group_id, period)
        if text is None:
            return await ctx.send(
                "This clan doesn't have a Clan Log yet — it's built the first time "
                "we sweep your drops. Ask an admin to enable it, or check back shortly.",
                ephemeral=True,
            )

        components = ActionRow(
            Button(label="Open the full board", style=ButtonStyle.URL,
                   url=board_url(group.group_id))
        )
        if file is not None:
            return await ctx.send(content=text, files=file, components=components)
        return await ctx.send(content=text, components=components)
