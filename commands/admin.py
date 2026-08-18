"""
Admin Commands Module

Contains Discord slash commands that require administrator permissions.
These commands handle group management, webhooks, and administrative functions.

Classes:
    ClanCommands: Extension containing admin-level slash commands

Author: joelhalen
"""

import json
import random
from datetime import datetime
from interactions import (
    SlashContext, Embed, OptionType, Extension, slash_command, slash_option,
    Permissions, GuildText, File
)
from sqlalchemy import delete
from db.clan_sync import insert_xf_group
from db.group_creation import create_web_group
from db.models import (
    Session, User, Group, Guild, GroupConfiguration, GroupEmbed, GroupPatreon, 
    GroupRecentDrops, NotificationQueue, NotifiedSubmission, PlayerPoints, Webhook, 
    user_group_association, session
)
from services.components import build_player_setup
from utils.format import get_command_id
from .utils import try_create_user, is_admin


class ClanCommands(Extension):
    """
    Extension containing administrator-level Discord slash commands.
    
    This extension provides commands that require administrator permissions
    and handle group management, webhook creation, and other admin functions.
    """

    @slash_command(name="create-group",
                    description="Create a new group with the DropTracker",
                    default_member_permissions=Permissions.ADMINISTRATOR)
    @slash_option(name="group_name",
                  opt_type=OptionType.STRING,
                  description="How would you like your group's name to appear?",
                  required=True)
    @slash_option(name="wom_id",
                  opt_type=OptionType.STRING,
                  description="Enter your group's WiseOldMan group ID",
                  max_length=6,
                  min_length=3,
                  required=True)
    async def create_group_cmd(self, ctx: SlashContext, group_name: str, wom_id: str):
        """
        Create a new DropTracker group linked to a Discord server.
        
        Creates a new group entry in the database, links it to the current Discord server,
        and sets up default configurations. Requires administrator permissions.
        
        Args:
            ctx (SlashContext): The slash command context
            group_name (str): Display name for the group
            wom_id (str): Wise Old Man group ID (3-6 digits)
        """
        await ctx.defer(ephemeral=True)
        try:
            wom_id = int(wom_id)
        except Exception as e:
            return await ctx.send(f"Please enter your WOM group ID with no commas or special characters. It should be only 3-6 digits long.")

        if not ctx.guild_id:
            return await ctx.send(f"You must use this command in a Discord server")

        if await is_admin(ctx):
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
            if not user:
                # Bot-side creation keeps the Discord "registered" role + DM
                # flow; the shared service only mirrors the DB portion.
                await try_create_user(ctx=ctx)

            # Shared creation logic (also behind the website wizard + legacy
            # XF intake): guild/WOM conflict checks, group + config-template
            # clone, group_admins owner seed, XF mirror, ticker, initial WOM
            # membership sync.
            result = await create_web_group(
                group_name=group_name,
                wom_id=wom_id,
                guild_id=str(ctx.guild_id),
                owner_discord_id=str(ctx.author_id),
                owner_username=ctx.author.username if ctx.author else None,
            )
            status = result.get("status")

            if status == "already_registered":
                return await ctx.send(f"You have already registered this group with the DropTracker! Please continue to [the website](https://www.droptracker.io/groups/{result.get('group_id')}) to configure your group.")
            if status == "guild_conflict":
                return await ctx.send(f"This Discord server is already associated with a DropTracker group (using wom id {result.get('wom_id')}).\\n" +
                                    "If this is a mistake, please reach out in Discord", ephemeral=True)
            if status == "wom_conflict":
                return await ctx.send(f"This WOM group (`{wom_id}`) already exists in our database.\\n" +
                                    "Please reach out in our Discord server if this appears to be a mistake.",
                                    ephemeral=True)
            if status == "invalid_name":
                return await ctx.send(f"Group names must be 1-30 characters long. Please try again with a shorter name.",
                                    ephemeral=True)
            if status == "invalid_wom":
                return await ctx.send(f"Please enter your WOM group ID with no commas or special characters. It should be only 3-6 digits long.")
            if status != "created":
                return await ctx.send(f"Unable to create your group due to a database error.\\n" +
                                    f"Please try again later or reach out in the DropTracker Discord server.",
                                    ephemeral=True)

            embed = Embed(title="New group created",
                        description=f"Your group has been created (ID: `{result.get('group_id')}`)!")
            embed.add_field(name=f"WOM group `{result.get('wom_id')}` is now assigned to your Discord server `{result.get('guild_id')}`",
                            value=f"<a:loading:1180923500836421715> Please wait while we initialize some other things for you...",
                            inline=False)
            embed.set_footer(f"https://discord.gg/droptracker")

            await ctx.send(f"Success!\\n", embed=embed, ephemeral=True)

            if "could not be applied" in (result.get("message") or ""):
                await ctx.send(f"⚠️ Your group was created successfully, but there was an issue setting up default configurations.\\n" +
                              f"Please visit the website to configure your group settings manually: https://www.droptracker.io/login",
                              ephemeral=True)

            await ctx.send("To continue setting up — channels, notification "
                           "toggles, thresholds — run `/group-setup` right here, "
                           "or [sign in on the website](https://www.droptracker.io/login) "
                           "for the full dashboard.",
                           ephemeral=True)
        else:
            await ctx.send(f"You do not have the necessary permissions to use this command inside of this Discord server.\\n" +
                           "Please ask the server owner to execute this command.",
                           ephemeral=True)

    @slash_command(name="dm-broken-groups",
                   description="Send a DM to administrators of groups that are not properly configured yet.",
                   default_member_permissions=Permissions.ADMINISTRATOR)
    async def dm_broken_groups(self, ctx: SlashContext):
        """
        Send DMs to administrators of improperly configured groups.
        
        Identifies groups with broken configurations and sends warning messages
        to their Discord server owners. Restricted to bot owner only.
        
        Args:
            ctx (SlashContext): The slash command context
        """
        if str(ctx.user.id) != "528746710042804247":
            return await ctx.send("You are not authorized to use this command.", ephemeral=True)
        await ctx.defer(ephemeral=True)

        # ORM-based query to find guilds with broken configuration
        # Subquery for lootboard_channel_id = '0'
        lootboard_subq = (
            session.query(GroupConfiguration.group_id)
            .filter(
                GroupConfiguration.config_key == 'lootboard_channel_id',
                GroupConfiguration.config_value == '0'
            )
            .subquery()
        )

        # Subquery for authed_users = '[]'
        authed_users_subq = (
            session.query(GroupConfiguration.group_id)
            .filter(
                GroupConfiguration.config_key == 'authed_users',
                GroupConfiguration.config_value == '[]'
            )
            .subquery()
        )

        # Intersect the two subqueries to get group_ids that match both
        broken_group_ids = (
            session.query(Guild.guild_id)
            .join(lootboard_subq, Guild.group_id == lootboard_subq.c.group_id)
            .join(authed_users_subq, Guild.group_id == authed_users_subq.c.group_id)
            .distinct()
            .all()
        )
        print("Got broken group ids:", broken_group_ids)

        async def create_dm_notice(bot) -> Embed:
            """Create the DM notice embed for broken groups."""
            embed_title = f"⚠️ **NOTICE** ⚠️"
            embed = Embed(title=embed_title, color=0x00ff00, timestamp=datetime.now())
            
            description_parts = []
            description_parts.append(f"### :rotating_light: **Your registered group with the DropTracker has been flagged as improperly configured, or not set up at all.**")
            description_parts.append("You will have a total of 7 days from the time this message was sent to set our Discord bot up.")
            description_parts.append("-# __If you don't act before then__, **all of your group data will be wiped & the bot will leave your guild**!\\n\\n")
            description_parts.append("**If you need help:**")
            description_parts.append("- Join our [discord server](https://discord.gg/droptracker)")
            description_parts.append(f"- Try the </help:{await get_command_id(bot, 'help')}> command")
            description_parts.append("You can also optionally remove our bot from your server now, if you decide you don't want to use it.")
            description_parts.append("**-# We contacted you because you were the owner of the discord guild we were added to.\\nThank you for your time!**")
            embed.description = "\\n".join(description_parts).strip()
            embed.set_footer(text=f"Powered by the DropTracker | https://www.droptracker.io/", icon_url="https://www.droptracker.io/img/droptracker-small.gif")
            return embed

        for guild_id_tuple in broken_group_ids:
            guild_id = guild_id_tuple[0]
            guild = await ctx.bot.fetch_guild(guild_id)
            if guild:
                continue  # TODO - don't continue if guild is found once we delete old data
                try:
                    guild_owner = await self.bot.fetch_user(guild._owner_id)
                    await guild_owner.send(content=f"## Hey, <@{guild._owner_id}>!", embed=await create_dm_notice(ctx.bot))
                except Exception as e:
                    print("Couldn't send DM to guild owner:", e)
            else:
                try:
                    group_id_row = session.query(Guild.group_id).filter(Guild.guild_id == guild_id).first()
                    group_id = group_id_row[0] if group_id_row else None
                    if not group_id:
                        continue
                        
                    # Prevent premature autoflush while we clean up
                    with session.no_autoflush:
                        # Delete association/dependent rows first
                        session.execute(delete(user_group_association).where(user_group_association.c.group_id == group_id))
                        session.execute(delete(NotificationQueue).where(NotificationQueue.group_id == group_id))
                        session.execute(delete(NotifiedSubmission).where(NotifiedSubmission.group_id == group_id))
                        session.execute(delete(GroupEmbed).where(GroupEmbed.group_id == group_id))
                        session.execute(delete(GroupPatreon).where(GroupPatreon.group_id == group_id))
                        session.execute(delete(GroupRecentDrops).where(GroupRecentDrops.group_id == group_id))
                        # Also remove group configuration to avoid FK updates to NULL on flush
                        session.execute(delete(GroupConfiguration).where(GroupConfiguration.group_id == group_id))

                        # Now delete ORM parents
                        group = session.query(Group).filter(Group.guild_id == guild_id).first()
                        if group:
                            session.delete(group)
                        guild_obj = session.query(Guild).filter(Guild.guild_id == guild_id).first()
                        if guild_obj:
                            session.delete(guild_obj)
                    session.commit()
                    await ctx.channel.send(f"Guild with id `{guild_id}` not found & is likely safe to be removed.")
                except Exception as e:
                    session.rollback()
                    await ctx.channel.send(f"Cleanup failed for guild `{guild_id}`: {e}")

    @slash_command(
        name="reset-group-points",
        description="Reset your server group's points back to zero",
        default_member_permissions=Permissions.ADMINISTRATOR
    )
    @slash_option(
        name="confirm_text",
        opt_type=OptionType.STRING,
        description="Type RESET to confirm this irreversible action",
        required=True
    )
    async def reset_group_points_cmd(self, ctx: SlashContext, confirm_text: str):
        if not ctx.guild_id:
            return await ctx.send("Use this command inside your group's Discord server.", ephemeral=True)

        if str(confirm_text).strip().upper() != "RESET":
            return await ctx.send(
                "Confirmation failed. Please run the command again and set `confirm_text` to `RESET`.",
                ephemeral=True
            )

        guild = session.query(Guild).filter(Guild.guild_id == str(ctx.guild_id)).first()
        if not guild or not guild.group_id:
            return await ctx.send("This server is not linked to a DropTracker group.", ephemeral=True)

        group = session.query(Group).filter(Group.group_id == guild.group_id).first()
        if not group:
            return await ctx.send("Group record was not found for this server.", ephemeral=True)

        try:
            removed_count = (
                session.query(PlayerPoints)
                .filter(PlayerPoints.group_id == group.group_id)
                .count()
            )
            (
                session.query(PlayerPoints)
                .filter(PlayerPoints.group_id == group.group_id)
                .delete(synchronize_session=False)
            )
            session.commit()
        except Exception as e:
            session.rollback()
            return await ctx.send(f"Failed to reset points: {e}", ephemeral=True)

        embed = Embed(
            title="Group Points Reset",
            description=(
                f"All points for **{group.group_name}** were reset to zero.\n"
                f"Removed **{removed_count:,}** award row(s)."
            ),
            color=0xE74C3C
        )
        embed.set_footer(text="This action is irreversible.")
        await ctx.send(embed=embed, ephemeral=True)

    @slash_command(
        name="force-group-sync",
        description="Force a WiseOldMan membership sync for one group",
        default_member_permissions=Permissions.ADMINISTRATOR
    )
    @slash_option(
        name="wom_id",
        opt_type=OptionType.STRING,
        description="Optional WOM group ID. If omitted, uses this server's linked group.",
        required=False
    )
    async def force_group_sync_cmd(self, ctx: SlashContext, wom_id: str = None):
        if wom_id:
            try:
                target_wom_id = int(str(wom_id).strip())
            except Exception:
                return await ctx.send(
                    "Invalid `wom_id`. Please provide digits only.",
                    ephemeral=True
                )
            target_group = session.query(Group).filter(Group.wom_id == target_wom_id).first()
            if not target_group:
                return await ctx.send(
                    f"No DropTracker group found with WOM ID `{target_wom_id}`.",
                    ephemeral=True
                )
        else:
            if not ctx.guild_id:
                return await ctx.send(
                    "When not providing `wom_id`, this command must be used in a server linked to a group.",
                    ephemeral=True
                )
            guild = session.query(Guild).filter(Guild.guild_id == str(ctx.guild_id)).first()
            if not guild or not guild.group_id:
                return await ctx.send(
                    "This Discord server is not linked to a DropTracker group.",
                    ephemeral=True
                )
            target_group = session.query(Group).filter(Group.group_id == guild.group_id).first()
            if not target_group or not target_group.wom_id:
                return await ctx.send(
                    "A linked group exists, but it has no WOM ID configured.",
                    ephemeral=True
                )
            target_wom_id = int(target_group.wom_id)

        await ctx.defer(ephemeral=True)
        started_at = datetime.now()
        try:
            # Import lazily to avoid unnecessary startup overhead for command modules.
            from db.ops import update_group_members_silent
            await update_group_members_silent(forced_id=target_wom_id)
            session.refresh(target_group)
            member_count = target_group.get_player_count()
        except Exception as e:
            session.rollback()
            print(f"Force sync failed for WOM group {target_wom_id}: {e}")
            return await ctx.send(
                f"Force sync failed for WOM group `{target_wom_id}` — please try again in a few minutes.",
                ephemeral=True
            )

        elapsed_seconds = max((datetime.now() - started_at).total_seconds(), 0.0)
        embed = Embed(
            title="Group Sync Completed",
            description=(
                f"Forced WOM sync finished for **{target_group.group_name}**.\n"
                f"- WOM ID: `{target_wom_id}`\n"
                f"- DropTracker Group ID: `{target_group.group_id}`\n"
                f"- Current tracked members: `{member_count}`\n"
                f"- Duration: `{elapsed_seconds:.1f}s`"
            ),
            color=0x2ECC71
        )
        await ctx.send(embed=embed, ephemeral=True)

    @slash_command(name="new_webhook",
                    description="Generate a new webhook, adding it to the database and the GitHub list.",
                    default_member_permissions=Permissions.ADMINISTRATOR)
    async def new_webhook_generator(self, ctx: SlashContext):
        """
        Generate new webhooks for the DropTracker system.
        
        Creates 30 new Discord webhooks across various channels and adds them
        to the database for use by the DropTracker system. Bot owner only.
        
        Args:
            ctx (SlashContext): The slash command context
        """
        if not str(ctx.user.id) == "528746710042804247":
            return await ctx.send("You are not authorized to use this command.", ephemeral=True)
        await ctx.defer(ephemeral=True)
        
        for i in range(30):
            with Session() as session:
                main_parent_ids = [1332506635775770624, 1332506742801694751, 1369779266945814569, 1369779329382482005, 1369803376598192128]
                hooks_parent_ids = [1332506904840372237, 1332506935886348339, 1369779098246975638, 1369779125035991171]
                hooks_2_parent_ids = [1369777536975900773, 1369777572577284167, 1369778911264641034, 1369778925919670432, 1369778911264641034]
                hooks_3_parent_ids = [1369780179064590418, 1369780228930670705, 1369780244583547073, 1369780261000183848, 1369780569080332369]

                all_parent_ids = main_parent_ids + hooks_parent_ids + hooks_2_parent_ids + hooks_3_parent_ids
                try:
                    parent_id = random.choice(all_parent_ids)
                    parent_channel = await ctx.bot.fetch_channel(parent_id)
                    num = 35
                    channel_name = f"drops-{num}"
                    while channel_name in [channel.name for channel in parent_channel.channels]:
                        num += 1
                        channel_name = f"drops-{num}"
                    new_channel: GuildText = await parent_channel.create_text_channel(channel_name)
                    logo_path = '/store/droptracker/disc/static/assets/img/droptracker-small.gif'
                    avatar = File(logo_path)
                    webhook = await new_channel.create_webhook(name=f"DropTracker Webhooks ({num})", avatar=avatar)
                    webhook_url = webhook.url
                    db_webhook = Webhook(webhook_id=str(webhook.id), webhook_url=str(webhook_url))
                    session.add(db_webhook)
                    session.commit()
                except Exception as e:
                    await ctx.send(f"Couldn't create a new webhook:{e}", ephemeral=True)
        print("Created 30 new webhooks.")

    @slash_command(name="send_player_faq",
                   description="Send a message from the DropTracker bot to help outline some player FAQs.",
                   default_member_permissions=Permissions.ADMINISTRATOR)
    async def send_player_faq_cmd(self, ctx: SlashContext):
        """
        Send a comprehensive FAQ message for players.
        
        Posts a detailed FAQ message with information about the DropTracker,
        how to get started, and common questions. Admin only.
        
        Args:
            ctx (SlashContext): The slash command context
        """
        # Reuse the shared FAQ builder so command mentions resolve dynamically
        # instead of relying on hardcoded (stale) command IDs.
        player_setup = await build_player_setup(self.bot)
        await ctx.channel.send(components=player_setup)

    @slash_command(
        name="toggle-split-tracking",
        description="Enable or disable split GP tracking for this server's group",
        default_member_permissions=Permissions.ADMINISTRATOR,
    )
    async def toggle_split_tracking_cmd(self, ctx: SlashContext):
        if not ctx.guild_id:
            return await ctx.send("Use this command inside your group's Discord server.", ephemeral=True)

        guild = session.query(Guild).filter(Guild.guild_id == str(ctx.guild_id)).first()
        if not guild or not guild.group_id:
            return await ctx.send("This server is not linked to a DropTracker group.", ephemeral=True)

        group = session.query(Group).filter(Group.group_id == guild.group_id).first()
        if not group:
            return await ctx.send("Group record was not found for this server.", ephemeral=True)

        try:
            existing = (
                session.query(GroupConfiguration)
                .filter(
                    GroupConfiguration.group_id == guild.group_id,
                    GroupConfiguration.config_key == "split_gp_tracking",
                )
                .first()
            )
            if existing:
                new_value = "0" if existing.config_value == "1" else "1"
                existing.config_value = new_value
            else:
                new_value = "1"
                session.add(GroupConfiguration(
                    group_id=guild.group_id,
                    config_key="split_gp_tracking",
                    config_value=new_value,
                ))
            session.commit()
        except Exception as e:
            session.rollback()
            print(f"Failed to toggle split tracking for group {guild.group_id}: {e}")
            return await ctx.send("Failed to update the setting — please try again in a few minutes.", ephemeral=True)

        state = "**enabled**" if new_value == "1" else "**disabled**"
        embed = Embed(
            title="Split GP Tracking Updated",
            description=(
                f"Split GP tracking is now {state} for **{group.group_name}**.\n\n"
                "When enabled, group leaderboard GP credit is distributed equally among "
                "all split participants rather than crediting the full value to the drop receiver."
            ),
            color=0x2ECC71 if new_value == "1" else 0xE74C3C,
        )
        await ctx.send(embed=embed, ephemeral=True)
