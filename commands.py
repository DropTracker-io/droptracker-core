from io import IOBase
import json
import os
import random
import re
from secrets import token_hex
from data.submissions import try_create_player
from db.clan_sync import insert_xf_group
from interactions import AutocompleteContext, BaseContext, GuildText, Permissions, SlashCommand, Button, ButtonStyle, SlashCommandOption, check, is_owner, Extension, slash_command, slash_option, SlashContext, Embed, OptionType, GuildChannel, SlashCommandChoice
import interactions
import time
import subprocess
import platform
from db.models import NpcList, Session, User, Group, Guild, Player, Drop, Webhook, session, UserConfiguration, GroupConfiguration
from pb.leaderboards import create_pb_embeds, get_group_pbs
from services import message_handler
from utils.format import format_time_since_update, format_number, get_command_id, get_npc_image_url, replace_placeholders
from utils.wiseoldman import check_user_by_id, check_user_by_username, check_group_by_id, fetch_group_members
from utils.redis import RedisClient
from db.ops import DatabaseOperations, associate_player_ids
from lootboard.generator import generate_server_board, generate_timeframe_board
from lootboard.player_board import generate_player_board
from datetime import datetime, timedelta
from utils.github import GithubPagesUpdater
import asyncio
from utils.sheets import sheet_manager
#from utils.zohomail import send_email
#from xf.xenforo import XenForoAPI
from sqlalchemy import text
#xf_api = XenForoAPI()
sheets = sheet_manager.SheetManager()
redis_client = RedisClient()
db = DatabaseOperations()


# Commands for the general user to interact with the bot
class UserCommands(Extension):
    def __init__(self, bot: interactions.Client):
        self.bot = bot
        self.message_handler = bot.get_ext("services.message_handler")

    @slash_command(name="help",
                   description="View helpful commands/links for the DropTracker")
    
    async def help(self, ctx):
        user = session.query(User).filter_by(discord_id=ctx.user.id).first()
        if not user:
            await try_create_user(ctx=ctx)
        user = session.query(User).filter(User.discord_id == ctx.author.id).first()
        help_embed = Embed(title="", description="", color=0x0ff000)

        help_embed.set_author(name="Help Menu",
                              url="https://www.droptracker.io/docs",
                              icon_url="https://www.droptracker.io/img/droptracker-small.gif")
        help_embed.set_thumbnail(url="https://www.droptracker.io/img/droptracker-small.gif")
        help_embed.add_field(name="Need more help?",
                            value=f"View our <#1317873428199637022> to find answers to common questions from our community, or reach out for <#1210765301042380820>")
        help_embed.add_field(name="User Commands:",
                             value="" +
                                   f"- </accounts:{await get_command_id(self.bot, 'accounts')}> - View which RuneScape accounts are associated with your Discord account.\n" +
                                   f"- </claim-rsn:{await get_command_id(self.bot, 'claim-rsn')}> - Claim a RuneScape character as one that belongs to you.\n")
        help_embed.add_field(name="Group Leader Commands:",
                             value="<:info:1263916332685201501> - `Note`: Creating groups **requires** a WiseOldMan group ID! *You can make a group without being in a clan*. [Visit the WOM website to create one](https://wiseoldman.net/groups/create).\n" +
                                   f"- </create-group:{await get_command_id(self.bot, 'create-group')}> - Create a new group in the DropTracker database to track your clan's drops.\n" +
                                   f"- </members:{await get_command_id(self.bot, 'members')}> - View a listing of the top members of your group in real-time.\n" +
                                   f"<:info:1263916332685201501> - All 'Group Leader Commands' require **Administrator** privileges in the Discord server you use them inside of.", inline=False)
        
        help_embed.add_field(name="Helpful Links",
                             value="[Docs](https://www.droptracker.io/docs) | "+
                             "[Join our Discord](https://www.droptracker.io/discord) | " +
                             "[GitHub](https://www.github.io/joelhalen/droptracker-py) | " + 
                             "[Patreon](https://www.patreon.com/droptracker)", inline=False)
        int_latency_ms = int(ctx.bot.latency * 1000)
        ext_latency_ms = await get_external_latency()
        help_embed.add_field(name="Latency",
                             value=f"Discord API: `{int_latency_ms} ms`\n" +
                                   f"External: `{ext_latency_ms} ms`", inline=False)

        return await ctx.send(embed=help_embed, ephemeral=True)
    @slash_command(name="global-board",
                   description="View the current global loot leaderboard")
    async def global_lootboard_cmd(self, ctx: SlashContext):
        embed = await db.get_group_embed(embed_type="lb", group_id=1)
        return await ctx.send(f"Here you are!", embeds=embed, ephemeral=True)
        pass

    @slash_command(name="pingme",
                   description="Toggle whether or not you want to be pinged when your submissions are sent to Discord")
    @slash_option(name="type",
                  description="Select whether you want to toggle global, or clan-specific pings.",
                  required=True,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    async def pingme_cmd(self, ctx: SlashContext, type: str):
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
        string_in = ctx.input_text
        await ctx.send(
            choices=[
                {
                    "name": f"Globally",
                    "value": "global"
                },
                {
                    "name": f"In my group",
                    "value": "group"
                },
                {
                    "name": f"Everywhere",
                    "value": "everywhere"
                }
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
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
        if account == "all":
            user.hidden = not user.hidden
            session.commit()
            if user.hidden:
                embed = Embed(title="Success!", 
                              description=f"All of your accounts will **no longer** be visible in our global listings.")
                return await ctx.send(embed=embed, ephemeral=True)
            else:
                embed = Embed(title="Success!",
                              description=f"All of your accounts will now **be visible** in our global listings.")
                return await ctx.send(embed=embed, ephemeral=True)
        else:
            player = session.query(Player).filter_by(player_name=account).first()
            if not player:
                return await ctx.send(f"You don't have any accounts by that name.", ephemeral=True)
            player.hidden = not player.hidden
            session.commit()
            if player.hidden:
                embed = Embed(title="Success!",
                              description=f"Your account, `{player.player_name}` will **no longer** be visible in our global listings.")
                return await ctx.send(embed=embed, ephemeral=True)
            else:
                embed = Embed(title="Success!",
                              description=f"Your account, `{player.player_name}` will now **be visible** in our global listings.")
                return await ctx.send(embed=embed, ephemeral=True)
            

    @hideme_cmd.autocomplete("account")
    async def hideme_autocomplete_account(self, ctx: AutocompleteContext):
        string_in = ctx.input_text
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        
        if not user:
            # User not found in database
            return await ctx.send(
                choices=[
                    {
                        "name": "All accounts",
                        "value": "all"
                    }
                ]
            )
        
        # Query for the user's accounts
        accounts = session.query(Player).filter_by(user_id=user.user_id).all()
        
        # Always include "All accounts" option
        choices = [
            {
                "name": "All accounts",
                "value": "all"
            }
        ]
        
        # Add player accounts if they exist
        if accounts:
            choices.extend([
                {
                    "name": account.player_name,
                    "value": account.player_name
                }
                for account in accounts
            ])
        
        return await ctx.send(choices=choices)
    
    @slash_command(name="group-board",
                   description="View the current group lootboard")
    @slash_option(name="start_time",
                  description="Select the start time you want to view the lootboard for.",
                  required=False,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    @slash_option(name="end_time",
                  description="Select the end time you want to view the lootboard for.",
                  required=False,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    @slash_option(name="npc",
                  description="Select the NPC you want to generate a board for exclusively.",
                  required=False,
                  opt_type=OptionType.INTEGER,
                  autocomplete=True)
    async def group_lootboard_cmd(self, ctx: SlashContext, start_time: str = None, end_time: str = None, npc: int = None):
        message_cont = ""
        
        # Parse start_time
        if start_time is None or start_time == "now":
            start_datetime = datetime.now() - timedelta(days=7)  # Default to 7 days ago
        elif start_time == "today":
            start_datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "yesterday":
            start_datetime = (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "week":
            start_datetime = (datetime.now() - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "month":
            start_datetime = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "year":
            start_datetime = datetime.now().replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            try:
                # Try to parse custom date format (YYYY-MM-DD)
                start_datetime = datetime.strptime(start_time, "%Y-%m-%d")
            except ValueError:
                start_datetime = datetime.now() - timedelta(days=7)
                message_cont += "Invalid start time format. Using default (7 days ago).\n"
        
        # Parse end_time
        if end_time is None or end_time == "now":
            end_datetime = datetime.now()
        elif end_time == "today":
            end_datetime = datetime.now().replace(hour=23, minute=59, second=59)
        elif end_time == "yesterday":
            end_datetime = (datetime.now() - timedelta(days=1)).replace(hour=23, minute=59, second=59)
        elif end_time == "week":
            # End of the current week (Sunday)
            today = datetime.now()
            days_until_sunday = 6 - today.weekday()  # 6 is Sunday in Python's weekday() (0-6, Monday is 0)
            end_datetime = (today + timedelta(days=days_until_sunday)).replace(hour=23, minute=59, second=59)
        elif end_time == "month":
            # End of the current month
            today = datetime.now()
            next_month = today.replace(day=28) + timedelta(days=4)  # This will never fail
            end_datetime = next_month.replace(day=1, hour=0, minute=0, second=0) - timedelta(seconds=1)
        else:
            try:
                # Try to parse custom date format (YYYY-MM-DD)
                end_datetime = datetime.strptime(end_time, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except ValueError:
                end_datetime = datetime.now()
                message_cont += "Invalid end time format. Using current time.\n"
        
        # Get group information
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        if not user:
            return await ctx.send(f"You have not yet registered an account in our database! Try registering first:\n" + 
                                 f"</claim-rsn:{await get_command_id(self.bot, 'claim-rsn')}>")
        group = None
        if ctx.guild:
            guild_id = ctx.guild_id
            if str(guild_id) == "1172737525069135962":
                group_id = 2
            else:
                group = session.query(Group).filter_by(guild_id=guild_id).first()
        if not group:
            group = session.query(Group).filter_by(group_id=2).first()
        
        group_id = group.group_id
        group_name = group.group_name
        
        # Show loading message
        await ctx.defer()
        
        # Get NPC name if specified
        npc_name = None
        if npc:
            npc_obj = session.query(NpcList).filter(NpcList.npc_id == npc).first()
            if npc_obj:
                npc_name = npc_obj.npc_name
        
        # Generate the lootboard
        board = await generate_timeframe_board(
            self.bot, 
            group_id=group_id,
            start_time=start_datetime,
            end_time=end_datetime,
            npc_id=npc
        )
        print("Board generation called for", group_id, "and npc id", npc)
        
        if board:
            lootboard = interactions.File(board)
            embed_template = await db.get_group_embed(embed_type="lb", group_id=group_id)
            
            if group_id != 2:
                player_wom_ids = await fetch_group_members(group.wom_id)
                player_ids = await associate_player_ids(player_wom_ids)
                total_tracked = len(player_ids)
            else:
                total_tracked = session.query(Player.wom_id).count()
            next_update = datetime.now() + timedelta(minutes=10)
            future_timestamp = int(time.mktime(next_update.timetuple()))
            value_dict = {
                "{next_refresh}": f"<t:{future_timestamp}:R>",
                "{tracked_members}": total_tracked
            }
            embed = replace_placeholders(embed_template, value_dict)
            true_embed = Embed(title=embed.title, description=embed.description)
            for field in embed.fields:
                if field.name != "Refreshes" and not str(field.value).startswith("<t:"):
                    true_embed.add_field(name=field.name, value=field.value,inline=field.inline)
            await ctx.send(embed=true_embed, files=lootboard)
        else:
            await ctx.send(f"An error occurred while generating the group lootboard. Please try again later.")

    @group_lootboard_cmd.autocomplete("start_time")
    async def group_lootboard_autocomplete_start_time(self, ctx: AutocompleteContext):
        # Get current date for custom options
        now = datetime.now()
        yesterday = now - timedelta(days=1)
        last_week = now - timedelta(days=7)
        last_month = now - timedelta(days=30)
        
        # Create standard choices
        choices = [
            {
                "name": "Today (midnight)",
                "value": "today"
            },
            {
                "name": "Yesterday (midnight)",
                "value": "yesterday"
            },
            {
                "name": "Last 7 days",
                "value": "week"
            },
            {
                "name": "This month (from 1st)",
                "value": "month"
            },
            {
                "name": "This year (from Jan 1st)",
                "value": "year"
            },
            {
                "name": f"Custom: {yesterday.strftime('%Y-%m-%d')}",
                "value": yesterday.strftime("%Y-%m-%d")
            },
            {
                "name": f"Custom: {last_week.strftime('%Y-%m-%d')}",
                "value": last_week.strftime("%Y-%m-%d")
            },
            {
                "name": f"Custom: {last_month.strftime('%Y-%m-%d')}",
                "value": last_month.strftime("%Y-%m-%d")
            }
        ]
        
        # Filter choices based on user input
        if ctx.input_text:
            filtered_choices = [
                choice for choice in choices 
                if ctx.input_text.lower() in choice["name"].lower() or ctx.input_text.lower() in choice["value"].lower()
            ]
            
            # Add custom date if it looks like a date format
            if re.match(r"\d{4}-\d{2}-\d{2}", ctx.input_text) or re.match(r"\d{2}-\d{2}-\d{4}", ctx.input_text):
                filtered_choices.append({
                    "name": f"Custom date: {ctx.input_text}",
                    "value": ctx.input_text
                })
            
            await ctx.send(choices=filtered_choices[:25])  # Discord limits to 25 choices
        else:
            await ctx.send(choices=choices[:25])

    @group_lootboard_cmd.autocomplete("end_time")
    async def group_lootboard_autocomplete_end_time(self, ctx: AutocompleteContext):
        # Get current date for custom options
        now = datetime.now()
        yesterday = now - timedelta(days=1)
        
        # Create standard choices
        choices = [
            {
                "name": "Now (current time)",
                "value": "now"
            },
            {
                "name": "Today (end of day)",
                "value": "today"
            },
            {
                "name": "Yesterday (end of day)",
                "value": "yesterday"
            },
            {
                "name": "End of this week",
                "value": "week"
            },
            {
                "name": "End of this month",
                "value": "month"
            },
            {
                "name": f"Custom: {now.strftime('%Y-%m-%d')}",
                "value": now.strftime("%Y-%m-%d")
            },
            {
                "name": f"Custom: {yesterday.strftime('%Y-%m-%d')}",
                "value": yesterday.strftime("%Y-%m-%d")
            }
        ]
        
        # Filter choices based on user input
        if ctx.input_text:
            filtered_choices = [
                choice for choice in choices 
                if ctx.input_text.lower() in choice["name"].lower() or ctx.input_text.lower() in choice["value"].lower()
            ]
            
            # Add custom date if it looks like a date format
            if re.match(r"\d{4}-\d{2}-\d{2}", ctx.input_text) or re.match(r"\d{2}-\d{2}-\d{4}", ctx.input_text):
                filtered_choices.append({
                    "name": f"Custom date: {ctx.input_text}",
                    "value": ctx.input_text
                })
            
            await ctx.send(choices=filtered_choices[:25])  # Discord limits to 25 choices
        else:
            await ctx.send(choices=choices[:25])

    @group_lootboard_cmd.autocomplete("npc")
    async def group_lootboard_autocomplete_npc(self, ctx: AutocompleteContext):
        # List of popular NPCs with their IDs
        popular_npcs = [
                (8615, "Alchemical Hydra"),
                (13668, "Araxxor"),
                (11175, "Araxyte"),
                (11992, "Artio"),
                (13729, "Barrows"),
                (8195, "Bryophyta"),
                (6503, "Callisto"),
                (11993, "Calvar'ion"),
                (5862, "Cerberus"),
                (13696, "Chambers of Xeric"),
                (6619, "Chaos Fanatic"),
                (13948, "Clue Scroll (Beginner)"),
                (13947, "Clue Scroll (Easy)"),
                (13944, "Clue Scroll (Elite)"),
                (13945, "Clue Scroll (Hard)"),
                (13955, "Clue Scroll (Master)"),
                (13946, "Clue Scroll (Medium)"),
                (13979, "Coffin (Hallowed Sepulchre)"),
                (2205, "Commander Zilyana"),
                (319, "Corporeal Beast"),
                (6618, "Crazy archaeologist"),
                (2267, "Dagannoth Rex"),
                (2265, "Dagannoth Supreme"),
                (7144, "Demonic gorilla"),
                (13680, "Dreadborn Araxyte"),
                (12191, "Duke Sucellus"),
                (7851, "Dusk"),
                (13709, "Elven Crystal Chest"),
                (13741, "Fortis Colosseum"),
                (2215, "General Graardor"),
                (13701, "Herbiboar"),
                (8583, "Hespori"),
                (8609, "Hydra"),
                (3129, "K'ril Tsutsaroth"),
                (963, "Kalphite Queen"),
                (239, "King Black Dragon"),
                (13684, "Kingdom of Miscellania"),
                (3162, "Kree'arra"),
                (13718, "Larran's big chest"),
                (11278, "Nex"),
                (12077, "Phantom Muspah"),
                (9416, "Phosani's Nightmare"),
                (303031, "Revenants"),
                (13954, "Reward pool (Tempoross)"),
                (7286, "Skotizo"),
                (7541, "Tekton"),
                (7543, "Tekton (enraged)"),
                (13703, "The Gauntlet"),
                (13949, "The Hueycoatl"),
                (12214, "The Leviathan"),
                (9425, "The Nightmare"),
                (12204, "The Whisperer"),
                (13699, "Theatre of Blood"),
                (499, "Thermonuclear smoke devil"),
                (13695, "Tombs of Amascut"),
                (1676, "Torag the Corrupted"),
                (13599, "Tormented Demon"),
                (13711, "Unsired"),
                (12223, "Vardorvis"),
                (6504, "Venenatis"),
                (6611, "Vet'ion"),
                (8060, "Vorkath"),
                (9049, "Zalcano"),
                (2042, "Zulrah")
            ]
        
        # Filter NPCs based on input text
        if ctx.input_text:
            filtered_npcs = []
            for npc_id, npc_name in popular_npcs:
                if ctx.input_text.lower() in npc_name.lower():
                    filtered_npcs.append((npc_id, npc_name))
            
            # Limit to 25 choices for Discord's autocomplete
            if len(filtered_npcs) > 25:
                filtered_npcs = filtered_npcs[:25]
        else:
            # Use all popular NPCs if no input, limited to 25
            filtered_npcs = popular_npcs[:25]
        
        # Format choices correctly for autocomplete
        choices = []
        for npc_id, npc_name in filtered_npcs:
            choices.append({
                "name": npc_name,
                "value": npc_id
            })
        
        await ctx.send(choices=choices)

    @slash_command(name="my-board",
                   description="View your personal lootboard")
    @slash_option(name="start_time",
                  description="Select the start time you want to view the lootboard for.",
                  required=False,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    @slash_option(name="end_time",
                  description="Select the end time you want to view the lootboard for.",
                  required=False,
                  opt_type=OptionType.STRING,
                  autocomplete=True)
    async def my_board_cmd(self, ctx: SlashContext, start_time: str = None, end_time: str = None):
        user = ctx.author
        user_id = ctx.author.id
        user = session.query(User).filter_by(discord_id=str(user_id)).first()
        message_cont = ""
        
        # Parse start_time
        if start_time is None or start_time == "now":
            start_datetime = datetime.now() - timedelta(days=7)  # Default to 7 days ago
        elif start_time == "today":
            start_datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "yesterday":
            start_datetime = (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "week":
            start_datetime = (datetime.now() - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "month":
            start_datetime = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif start_time == "year":
            start_datetime = datetime.now().replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            try:
                # Try to parse custom date format (YYYY-MM-DD)
                start_datetime = datetime.strptime(start_time, "%Y-%m-%d")
            except ValueError:
                start_datetime = datetime.now() - timedelta(days=7)
                message_cont += "Invalid start time format. Using default (7 days ago).\n"
        
        # Parse end_time
        if end_time is None or end_time == "now":
            end_datetime = datetime.now()
        elif end_time == "today":
            end_datetime = datetime.now().replace(hour=23, minute=59, second=59)
        elif end_time == "yesterday":
            end_datetime = (datetime.now() - timedelta(days=1)).replace(hour=23, minute=59, second=59)
        elif end_time == "week":
            # End of the current week (Sunday)
            today = datetime.now()
            days_until_sunday = 6 - today.weekday()  # 6 is Sunday in Python's weekday() (0-6, Monday is 0)
            end_datetime = (today + timedelta(days=days_until_sunday)).replace(hour=23, minute=59, second=59)
        elif end_time == "month":
            # End of the current month
            today = datetime.now()
            next_month = today.replace(day=28) + timedelta(days=4)  # This will never fail
            end_datetime = next_month.replace(day=1, hour=0, minute=0, second=0) - timedelta(seconds=1)
        else:
            try:
                # Try to parse custom date format (YYYY-MM-DD)
                end_datetime = datetime.strptime(end_time, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except ValueError:
                end_datetime = datetime.now()
                message_cont += "Invalid end time format. Using current time.\n"
        
        if not user:
            return await ctx.send(f"You have not yet registered an account in our database! Try registering first:\n" + 
                                  f"</claim-rsn:{await get_command_id(self.bot, 'claim-rsn')}>")
        print("User found:", user.user_id)
        players = session.query(Player).filter_by(user_id=user.user_id).all()
        if len(players) > 1:
            message_cont += "You have multiple accounts registered in our database. We are using the first account you registered."
            player = players[0]
        else:
            player = players[0]
        # Show loading message
        await ctx.defer()
        
        player_board = await generate_player_board(self.bot, player.player_id, start_datetime, end_datetime)
        if player_board:
            lootboard = interactions.File(player_board)
            embed = Embed(title="Your Personal Lootboard", description=message_cont if message_cont else None)
            embed.set_footer(text="Powered by the DropTracker | https://www.droptracker.io/")
            embed.set_thumbnail(url="https://www.droptracker.io/img/droptracker-small.gif")
            
            # Format dates for display
            start_str = start_datetime.strftime("%Y-%m-%d %H:%M")
            end_str = end_datetime.strftime("%Y-%m-%d %H:%M")
            
            embed.add_field(
                name="Viewing a board for:",
                value=f"Player: `{player.player_name}`\n" + 
                      f"Timeframe: `{start_str}` to `{end_str}`"
            )
            await ctx.send(embed=embed, files=lootboard)
        else:
            await ctx.send(f"An error occurred while generating your lootboard. Please try again later.")
            
    @my_board_cmd.autocomplete("start_time")
    async def my_board_autocomplete_start_time(self, ctx: AutocompleteContext):
        # Get current date for custom options
        now = datetime.now()
        yesterday = now - timedelta(days=1)
        last_week = now - timedelta(days=7)
        last_month = now - timedelta(days=30)
        
        # Create standard choices
        choices = [
            {
                "name": "Today (midnight)",
                "value": "today"
            },
            {
                "name": "Yesterday (midnight)",
                "value": "yesterday"
            },
            {
                "name": "Last 7 days",
                "value": "week"
            },
            {
                "name": "This month (from 1st)",
                "value": "month"
            },
            {
                "name": "This year (from Jan 1st)",
                "value": "year"
            },
            {
                "name": f"Custom: {yesterday.strftime('%Y-%m-%d')}",
                "value": yesterday.strftime("%Y-%m-%d")
            },
            {
                "name": f"Custom: {last_week.strftime('%Y-%m-%d')}",
                "value": last_week.strftime("%Y-%m-%d")
            },
            {
                "name": f"Custom: {last_month.strftime('%Y-%m-%d')}",
                "value": last_month.strftime("%Y-%m-%d")
            }
        ]
        
        # Filter choices based on user input
        if ctx.input_text:
            filtered_choices = [
                choice for choice in choices 
                if ctx.input_text.lower() in choice["name"].lower() or ctx.input_text.lower() in choice["value"].lower()
            ]
            
            # Add custom date if it looks like a date format
            if re.match(r"\d{4}-\d{2}-\d{2}", ctx.input_text) or re.match(r"\d{2}-\d{2}-\d{4}", ctx.input_text):
                filtered_choices.append({
                    "name": f"Custom date: {ctx.input_text}",
                    "value": ctx.input_text
                })
            
            await ctx.send(choices=filtered_choices[:25])  # Discord limits to 25 choices
        else:
            await ctx.send(choices=choices[:25])

    @my_board_cmd.autocomplete("end_time")
    async def my_board_autocomplete_end_time(self, ctx: AutocompleteContext):
        # Get current date for custom options
        now = datetime.now()
        yesterday = now - timedelta(days=1)
        
        # Create standard choices
        choices = [
            {
                "name": "Now (current time)",
                "value": "now"
            },
            {
                "name": "Today (end of day)",
                "value": "today"
            },
            {
                "name": "Yesterday (end of day)",
                "value": "yesterday"
            },
            {
                "name": "End of this week",
                "value": "week"
            },
            {
                "name": "End of this month",
                "value": "month"
            },
            {
                "name": f"Custom: {now.strftime('%Y-%m-%d')}",
                "value": now.strftime("%Y-%m-%d")
            },
            {
                "name": f"Custom: {yesterday.strftime('%Y-%m-%d')}",
                "value": yesterday.strftime("%Y-%m-%d")
            }
        ]
        
        # Filter choices based on user input
        if ctx.input_text:
            filtered_choices = [
                choice for choice in choices 
                if ctx.input_text.lower() in choice["name"].lower() or ctx.input_text.lower() in choice["value"].lower()
            ]
            
            # Add custom date if it looks like a date format
            if re.match(r"\d{4}-\d{2}-\d{2}", ctx.input_text) or re.match(r"\d{2}-\d{2}-\d{4}", ctx.input_text):
                filtered_choices.append({
                    "name": f"Custom date: {ctx.input_text}",
                    "value": ctx.input_text
                })
            
            await ctx.send(choices=filtered_choices[:25])  # Discord limits to 25 choices
        else:
            await ctx.send(choices=choices[:25])
        
    @slash_command(name="accounts",
                   description="View your currently claimed RuneScape character names, if you have any")
    async def user_accounts_cmd(self, ctx):
        print("User accounts command...")
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
                account_names += f"`" + account.player_name.strip() + f"` (id: {account.player_id})\n> Last updated: {last_updated_unix}\n"
        account_emb = Embed(title="Your Registered Accounts:",
                            description=f"{account_names}(total: `{count}`)")
        # TODO - replace /claim-rsn with an actual clickable command
        account_emb.add_field(name="/claim-rsn",value="To claim another, you can use the </claim-rsn:1269466219841327108> command.", inline=False)
        account_emb.set_footer(text="https://www.droptracker.io/")
        await ctx.send(embed=account_emb, ephemeral=True)
    
    @slash_command(name="claim-rsn",
                    description="Claim ownership of your RuneScape account names in the DropTracker database")
    @slash_option(name="rsn",
                  opt_type=OptionType.STRING,
                  description="Please type the in-game-name of the account you want to claim, **exactly as it appears**!",
                  required=True)
    async def claim_rsn_command(self, ctx, rsn: str):
        user = session.query(User).filter_by(discord_id=str(ctx.user.id)).first()
        group = None
        if not user:
            await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
        if ctx.guild:
            guild_id = ctx.guild.id
            group = session.query(Group).filter(Group.guild_id.ilike(guild_id)).first()
        if not group:
            group = session.query(Group).filter_by(group_id=2).first()
        player = session.query(Player).filter(Player.player_name.ilike(rsn)).first()
        ## User should be made now
        if not player:
            try:
                wom_data = await check_user_by_username(rsn)
            except Exception as e:
                print("Couldn't get player data. e:", e)
                return await ctx.send(f"An error occurred claiming your account.\n" +
                                      "Try again later, or reach out in our Discord server",
                                      ephemeral=True)
            if wom_data:
                player, player_name, player_id, log_slots = wom_data
                try:
                    print("Creating a player with user ID", user.user_id, "associated with it")
                    ## We need to create the Player with a temporary acc hash for now
                    if group:
                        new_player = Player(wom_id=player_id, 
                                            player_name=rsn, 
                                            user_id=str(user.user_id), 
                                            user=user, 
                                            log_slots=log_slots,    
                                            group=group,
                                            account_hash=None)
                    else:
                        new_player = Player(wom_id=player_id, 
                                            player_name=rsn, 
                                            user_id=str(user.user_id), 
                                            log_slots=log_slots,
                                            account_hash=None,
                                            user=user)
                    session.add(new_player)
                    session.commit()
                except Exception as e:
                    print(f"Could not create a new player:", e)
                    session.rollback()
                finally:
                    return await ctx.send(f"Your account ({player_name}), with ID `{player_id}` has " +
                                         "been added to the database & associated with your Discord account.",ephemeral=True)
            else:
                return await ctx.send(f"Your account was not found in the WiseOldMan database.\n" +
                                     f"You could try to manually update your account on their website by [clicking here](https://www.wiseoldman.net/players/{rsn}), then try again, or wait a bit.")
        else:
            joined_time = format_time_since_update(player.date_added)
            if player.user:
                user: User = player.user
                if str(user.discord_id) != str(ctx.user.id):
                    await ctx.send(f"Uh-oh!\n" +
                                f"It looks like somebody else may have claimed your account {joined_time}!\n" +
                                f"<@{player.user.discord_id}> (discord id: {player.user.discord_id}) currently owns it in our database.\n" + 
                                "If this is some type of mistake, please reach out in our discord server:\n" + 
                                "https://www.droptracker.io/discord",
                                ephemeral=True)
                else:
                    await ctx.send(f"It looks like you've already claimed this account ({player.player_name}) {joined_time}\n" + 
                                "\nSomething not seem right?\n" +
                                "Please reach out in our discord server:\n" + 
                                "https://www.droptracker.io/discord",
                                ephemeral=True)
            else:
                player.user = user
                session.commit()
                embed = Embed(title="Success!",
                              description=f"Your in-game name has been successfully associated with your Discord account.\n" +
                              "That's it!") 
                embed.add_field(name=f"What's next?",value=f"If you'd like, you can [register an account on our website] to stay informed " + 
                                "on updates & to make your voice heard relating to bugs & suggestions.",inline=False)
                embed.set_thumbnail(url="https://www.droptracker.io/img/droptracker-small.gif")
                embed.set_footer(text="Powered by the DropTracker | https://www.droptracker.io/")
                await ctx.send(embed=embed)

    
    @slash_command(
        name="force_msg",
        description="Force a re-processing of a webhook message",
        default_member_permissions=Permissions.ADMINISTRATOR,
    )
    @slash_option(
        name="message_id",
        description="The message ID to re-process",
        opt_type=OptionType.STRING,
        required=True
    )
    @slash_option(
        name="channel_id",
        description="The channel ID the message is inside of",
        opt_type=OptionType.STRING,
        required=True
    )
    async def force_msg(self, ctx: SlashContext, channel_id: str, message_id: str):
        await ctx.send("Force message re-processing initiated.")
        #await message_data_logger.log("force_msg", {"message_id": ctx.message.id, "channel_id": ctx.channel.id})
        
        channel = await ctx.bot.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        if message:
            try:
                print("Re-processing message...")
                await self.message_handler.on_message_create(self.message_handler, message)
            except Exception as e:
                print("Error re-processing message:", e)
                await ctx.send(f"Error re-processing message: {e}")
        else:
            await ctx.send("Message not found.")


    @slash_command(name="new_webhook",
                    description="Generate a new webhook, adding it to the database and the GitHub list.",
                    default_member_permissions=Permissions.ADMINISTRATOR)
    async def new_webhook_generator(self, ctx: SlashContext):
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
                    avatar = interactions.File(logo_path)
                    webhook: interactions.Webhook = await new_channel.create_webhook(name=f"DropTracker Webhooks ({num})", avatar=avatar)
                    webhook_url = webhook.url
                    db_webhook = Webhook(webhook_id=str(webhook.id), webhook_url=str(webhook_url))
                    session.add(db_webhook)
                    session.commit()
                except Exception as e:
                    await ctx.send(f"Couldn't create a new webhook:{e}",ephemeral=True)
            pass
        print("Created 30 new webhooks.")





async def is_admin(ctx: BaseContext):
    perms_value = ctx.author.guild_permissions.value
    print("Guild permissions:", perms_value)
    if perms_value & 0x00000008:  # 0x8 is the bit flag for administrator
        return True
    return False






@slash_command(name="update_github",
               description="Force an immediate refresh of the GitHub webhooks",
               default_member_permissions=Permissions.ADMINISTRATOR)
async def update_github_cmd(self, ctx: SlashContext):
    GithubUpdater = GithubPagesUpdater()
    await ctx.send("Attempting to update the GitHub webhooks...", ephemeral=True)
    try:
        await GithubUpdater.update_github_pages()
        await asyncio.sleep(5)
        await ctx.send("GitHub webhooks updated", ephemeral=True)
    except Exception as e:
        #
        await ctx.send("An error occurred updating the GitHub webhooks", ephemeral=True)

# Commands that help configure or change clan-specifics.
class ClanCommands(Extension):
    @slash_command(name="create-group",
                    description="Create a new group with the DropTracker",
                    default_member_permissions=Permissions.ADMINISTRATOR)
    @slash_option(name="group_name",
                  opt_type=OptionType.STRING,
                  description="How would you like your group's name to appear?",
                  required=True)
    @slash_option(name="wom_id",
                  opt_type=OptionType.INTEGER,
                  description="Enter your group's WiseOldMan group ID",
                  required=True)
    async def create_group_cmd(self, 
                               ctx: SlashContext, 
                               group_name: str,
                               wom_id: int):
        if not ctx.guild:
            return await ctx.send(f"You must use this command in a Discord server")
        if ctx.author_permissions.ALL:
            print("Comparing:")
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
            if not user:
                await try_create_user(ctx=ctx)
            user = session.query(User).filter(User.discord_id == ctx.author.id).first()
            guild = session.query(Guild).filter(Guild.guild_id == ctx.guild_id).first()
            if not guild:
                guild = Guild(guild_id=str(ctx.guild_id),
                                  date_added=datetime.now())
                session.add(guild)
                session.commit()
            else:
                if guild.group_id != None:
                    return await ctx.send(f"This Discord server is already associated with a DropTracker group.\n" + 
                                        "If this is a mistake, please reach out in Discord", ephemeral=True)
        
            group = session.query(Group).filter(Group.wom_id == wom_id).first()
            if group:
                return await ctx.send(f"This WOM group (`{wom_id}`) already exists in our database.\n" + 
                                    "Please reach out in our Discord server if this appears to be a mistake.",
                                    ephemeral=True)
            else:
                group = Group(group_name=group_name,
                            wom_id=wom_id,
                            guild_id=guild.guild_id)
                session.add(group)
                print("Created a group")
                user.add_group(group)
                try:
                    group_wom_ids = await fetch_group_members(wom_id)
                    group_members = session.query(Player).filter(Player.wom_id.in_(group_wom_ids)).all()
                    for member in group_members:
                        if member.user:
                            user: User = member.user
                            user.add_group(group)
                        member.add_group(group)
                    total_members = len(group_wom_ids)
                    total_tracked_already = len(group_members)
                    session.commit()
                except Exception as e:
                    print("Error fetching group members/assigning them to the group during group creation:", e)
                    session.rollback()
            guild.group_id = group.group_id
            embed = Embed(title="New group created",
                        description=f"Your Group has been created (ID: `{group.group_id}`) with `{total_tracked_already}` DropTracker users already being tracked.")
            embed.add_field(name=f"WOM group `{group.wom_id}` (`{total_members}` members) is now assigned to your Discord server `{group.guild_id}`",
                            value=f"<a:loading:1180923500836421715> Please wait while we initialize some other things for you...",
                            inline=False)
            embed.set_footer(f"https://www.droptracker.io/discord")
            try:
                await insert_xf_group(group)
            except Exception as e:
                print(f"Error inserting group into XenForo: {e}")
            await ctx.send(f"Success!\n",embed=embed,
                                            ephemeral=True)
            default_config = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == 1).all()
            ## grab the default configuration options from the database
            new_config = []
            for option in default_config:
                option_value = option.config_value
                if option.config_key == "clan_name":
                    option_value = group_name
                if option.config_key == "authed_users":
                    option_value = f'["{str(ctx.author.id)}"]'
                default_option = GroupConfiguration(
                    group_id=group.group_id,
                    config_key=option.config_key,
                    config_value=option_value,
                    updated_at=datetime.now(),
                    group=group
                )
                new_config.append(default_option)
            try:
                session.add_all(new_config)
                session.commit()
            except Exception as e:
                session.rollback()
                print("Error occured trying to save configs::", e)
                return await ctx.send(f"Unable to create the default configuration options for your clan.\n" + 
                                      f"Please reach out in the DropTracker Discord server.",
                                      ephemeral=True)
                    # send_email(subject=f"New Group: {group_name}",
                    #         recipients=["support@droptracker.io"],
                    #         body=f"A new group was registered in the database." + 
                    #                 f"\nDropTracker Group ID: {group.group_id}\n" + 
                    #                 f"Discord server ID: {str(ctx.guild_id)}")
            await asyncio.sleep(5)
            await ctx.send(f"To continue setting up, please [sign in on the website](https://www.droptracker.io/login/discord)",
                            ephemeral=True)
        else:
            await ctx.send(f"You do not have the necessary permissions to use this command inside of this Discord server.\n" + 
                           "Please ask the server owner to execute this command.",
                           ephemeral=True)
            
    @slash_command(name="set-lootboard-style",
                   description="Set the style of the lootboard for a group")
    @slash_option(name="style",
                  description="Select which style of board you want to use.",
                  opt_type=OptionType.STRING,
                  choices=[
                    SlashCommandChoice(name="Dark style with outlines", value="1"), # bank-new-clean-dark.png
                    SlashCommandChoice(name="Light style with outlines", value="2"), # bank-new-clean.png
                    SlashCommandChoice(name="Dark style with no outlines", value="3"), # lootboard-newest.png
                    SlashCommandChoice(name="Dark/no outlines/no bg", value="4"), # no_boxes_dark.png
                    SlashCommandChoice(name="Minimal / no boxes", value="5"), # no_boxes_minimal.png
                    SlashCommandChoice(name="Halloween", value="6"), # halloween.png
                  ],
                  required=True)
    async def set_lootboard_style(self, ctx: SlashContext, style: str):

        if not ctx.guild:
            return await ctx.send("You must use this command in a Discord server.", ephemeral=True)
        if str(ctx.guild_id) == "1172737525069135962":
            group = session.query(Group).filter(Group.group_id == 2).first()
        else:
            group = session.query(Group).filter(Group.guild_id == str(ctx.guild_id)).first()
        if not group:
            return await ctx.send("No group found for this server.", ephemeral=True)
        user = session.query(User).filter(User.discord_id == str(ctx.author.id)).first()
        if not user:
            await try_create_user(ctx=ctx)
            return await ctx.send(f"You are not authorized to use this command in this group.", ephemeral=True)
        if not is_user_authorized(user.user_id, group):
            return await ctx.send(f"You are not authorized to use this command in this group.", ephemeral=True)
        current_style = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group.group_id, GroupConfiguration.config_key == "loot_board_type").first()
        if current_style:
            await ctx.send(f"Current value: {current_style.config_value} ({type(current_style.config_value)}) -> changing to {style} ({type(style)})")
            current_style.config_value = style
            session.commit()
            print(f"Committed style change for {group.group_id} to {style}")
        else:
            await ctx.send(f"Could not change your configured loot leaderboard style.\n" + 
                           "Please try again later, or reach out in our Discord server.",
                           ephemeral=True)
            return

        current_style = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group.group_id, GroupConfiguration.config_key == "loot_board_type").first()
        print("Current style:", current_style.config_value)
        match style:
            case "1":
                url = "/store/droptracker/disc/lootboard/bank-new-clean-dark.png"
            case "2":
                url = "/store/droptracker/disc/lootboard/bank-new-clean.png"
            case "3":
                url = "/store/droptracker/disc/lootboard/lootboard-newest.png"
            case "4":
                url = "/store/droptracker/disc/lootboard/no_boxes_dark.png"
            case "5":
                url = "/store/droptracker/disc/lootboard/no_boxes_minimal.png"
            case "6":
                url = "/store/droptracker/disc/lootboard/halloween.png"
        
        try:
            attachment = interactions.File(url)
            await ctx.send(f"Changed {group.group_name}'s lootboard style to `{style}`.", ephemeral=True, files=attachment)
        except Exception as e:
            await ctx.send(f"Changed {group.group_name}'s lootboard style to `{style}`.", ephemeral=True)


def is_user_authorized(user_id, group: Group):
    # Check if the user is an admin or an authorized user for this group
    group_config = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group.group_id).all()
    # Transform group_config into a dictionary for easy access
    config = {conf.config_key: conf.config_value for conf in group_config}
    authed_user = False
    user_data: User = session.query(User).filter(User.user_id == user_id).first()
    if user_data:
        discord_id = user_data.discord_id
    else:
        return False
    if "authed_users" in config:
        authed_users = config["authed_users"]
        if isinstance(authed_users, int):
            authed_users = f"{authed_users}"  # Get the list of authorized user IDs
        print("Authed users:", authed_users)
        authed_users = json.loads(authed_users)
        # Loop over authed_users and check if the current user is authorized
        for authed_id in authed_users:
            if str(authed_id) == str(discord_id):  # Compare the authed_id with the current user's ID
                authed_user = True
                return True  # Exit the loop once the user is found
    return authed_user

async def try_create_user(discord_id: str = None, username: str = None, ctx: SlashContext = None):
    if discord_id == None and username == None:
        if ctx:
            username = ctx.user.username
            discord_id = ctx.user.id
    user = None
    try:
        group = None
        if ctx:
            if ctx.guild_id:
                guild_ob = session.query(Guild).filter(Guild.guild_id == ctx.guild_id).first()
                if guild_ob:
                    group = session.query(Group).filter(Group.group_id == guild_ob.group_id).first()
        if group:
            new_user: User = User(auth_token="", discord_id=str(discord_id), username=str(username), groups=[group])
        else:
            new_user: User = User(auth_token="", discord_id=str(discord_id), username=str(username))
        if new_user:
            session.add(new_user)
            session.commit()

    except Exception as e:
        print("An error occured trying to add a new user to the database:", e)
        if ctx:
            return await ctx.author.send(f"An error occurred attempting to register your account in the database.\n" + 
                                    f"Please reach out for help: https://www.droptracker.io/discord",ephemeral=True)
    default_config = session.query(UserConfiguration).filter(UserConfiguration.user_id == 1).all()
    ## grab the default configuration options from the database
    if new_user:
        user = new_user
    if not user:
        user = session.query(User).filter(User.discord_id == discord_id).first()

    new_config = []
    for option in default_config:
        option_value = option.config_value
        default_option = UserConfiguration(
            user_id=user.user_id,
            config_key=option.config_key,
            config_value=option_value,
            updated_at=datetime.now()
        )
        new_config.append(default_option)
    try:
        session.add_all(new_config)
        session.commit()
    except Exception as e:
        session.rollback()
    try:
        droptracker_guild: interactions.Guild = await ctx.bot.fetch_guild(guild_id=1172737525069135962)
        dt_member = droptracker_guild.get_member(member_id=discord_id)
        if dt_member:
            registered_role = droptracker_guild.get_role(role_id=1210978844190711889)
            await dt_member.add_role(role=registered_role)
    except Exception as e:
        print("Couldn't add the user to the registered role:", e)
    # xf_user = await xf_api.try_create_xf_user(discord_id=str(discord_id),
    #                                 username=username,
    #                                 auth_key=str(auth_token))
    # if xf_user:
    #     user.xf_user_id = xf_user['user_id']
    session.commit()
    if ctx:
        claim_rsn_cmd_id = await get_command_id(ctx.bot, 'claim-rsn')
        cmd_id = str(claim_rsn_cmd_id)
        if str(ctx.command_id) != cmd_id:
            reg_embed=Embed(title="Account Registered",
                                 description=f"Your account has been created. (DT ID: `{user.user_id}`)")
            reg_embed.add_field(name="Please claim your accounts!",
                                value=f"The next thing you should do is " + 
                                f"use </claim-rsn:{await get_command_id(ctx.bot, 'claim-rsn')}>" + 
                                "for each of your in-game names, so you can associate them with your Discord account.",
                                inline=False)
            reg_embed.add_field(name="Change your configuration settings:",
                                value=f"Feel free to visit the website to configure privacy settings related to your drops & more",
                                inline=False)
            await ctx.send(embed=reg_embed,ephemeral=True)
            reg_embed=Embed(title="Account Registered",
                                 description=f"Your account has been created. (DT ID: `{user.user_id}`)")
            reg_embed.add_field(name="Change your configuration settings:",
                                value=f"Feel free to [sign in on the website](https://www.droptracker.io/) to configure your user settings.",
                                inline=False)
            return await ctx.send(embed=reg_embed)
        else:
            reg_embed=Embed(title="Account Registered",
                                 description=f"Your account has been created. (DT ID: `{user.user_id}`)")
            reg_embed.add_field(name="Change your configuration settings:",
                                value=f"Feel free to [sign in on the website](https://www.droptracker.io/) to configure your user settings.",
                                inline=False)
            await ctx.author.send(embed=reg_embed)
            return True
            
            

async def get_external_latency():
        host = "amazon.com"
        ping_command = ["ping", "-c", "1", host]

        try:
            output = subprocess.check_output(ping_command, stderr=subprocess.STDOUT, universal_newlines=True)
            if "time=" in output:
                ext_latency_ms = output.split("time=")[-1].split(" ")[0]
                return ext_latency_ms
        except subprocess.CalledProcessError:
            return "N/A"  

        return "N/A"