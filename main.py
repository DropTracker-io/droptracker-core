import threading
import aiohttp
from db.clan_sync import insert_xf_group
from h11 import LocalProtocolError
import interactions
import json
from dotenv import load_dotenv
import asyncio
import os
import time
import multiprocessing

from sqlalchemy import text
from utils.embeds import create_boss_pb_embed, update_boss_pb_embed
from utils.logger import LoggerClient

from multiprocessing import Value

from quart import Quart, abort, jsonify, request, session as quart_session, render_template
from quart_jwt_extended import (
    JWTManager,
    jwt_required,
    create_access_token,
    get_jwt_identity,
    verify_jwt_in_request,
    decode_token
)
from osrsreboxed import monsters_api, items_api
import hypercorn.asyncio
from interactions import Intents, user_context_menu, ContextMenuContext, Member, listen, Status, Task, IntervalTrigger, \
    ActivityType, ChannelType, slash_command, Embed, slash_option, OptionType, check, is_owner, \
    slash_default_member_permission, Permissions, SlashContext, ButtonStyle, Button, SlashCommand, ComponentContext, \
    component_callback, Modal, ShortText, BaseContext, Extension, GuildChannel
from interactions.api.events import GuildJoin, GuildLeft, MessageCreate, Component, Startup
from pb.leaderboards import create_pb_embeds
from lootboard.generator import generate_server_board
from utils.cloudflare_update import CloudflareIPUpdater
from utils.wiseoldman import fetch_group_members
from web.api import create_api
from web.front import create_frontend
from commands import UserCommands, ClanCommands
from tickets import Tickets
from db.models import Group, GroupConfiguration, GroupPatreon, GroupPersonalBestMessage, Guild, User, session, NpcList, ItemList, Webhook, Player
from db.update_player_total import start_background_redis_tasks
from db.ops import associate_player_ids, update_group_members
from db.ops import DatabaseOperations
from utils.messages import message_processor, joined_guild_msg
from utils.patreon import patreon_sync
from utils.redis import RedisClient, calculate_clan_overall_rank
from utils.download import download_player_image
from utils.github import GithubPagesUpdater
from data.submissions import ca_processor, drop_processor, pb_processor, clog_processor
from utils.format import get_sorted_doc_files, format_time_since_update, format_number, get_command_id, get_extension_from_content_type, convert_to_ms, replace_placeholders
from datetime import datetime, timedelta
import re
import logging
from games.gielinor_race.routes import gielinor_race_bp
#from xf.xenforo import XenForoAPI
#from web.admin_cp import create_admin_cp
#from xf_drop_conversion import transfer_drops
#xf_api = XenForoAPI()

bot_ready = Value('b', False)  # 'b' is for boolean
logger = LoggerClient(token=os.getenv('LOGGER_TOKEN'))
import warnings

db = DatabaseOperations()

## global variables modified throughout operation + accessed elsewhere ##
total_guilds = 0
total_users = 0
start_time: time = None
current_time = time.time()
redis_client = RedisClient()
## Category IDs that contain DropTracker webhooks that receive messages from the RuneLite client
load_dotenv()

# Hypercorn configuration
def create_hypercorn_config():
    config = hypercorn.Config()
    config.bind = ["127.0.0.1:8080"]  # Only bind to localhost since NGINX will proxy
    config.use_reloader = False
    config.worker_class = "asyncio"
    config.always_use_service_workers = True
    config.timeout = 60
    config.keep_alive_timeout = 75
    config.forwarded_allow_ips = "*"
    config.proxy_headers = True
    return config

## Discord Bot initialization ##

bot = interactions.Client(intents=Intents.ALL,
                          send_command_traceback=False,
                          owner_ids=[528746710042804247, 232236164776460288])
bot.send_not_ready_messages = True

bot_token = os.getenv('BOT_TOKEN')

## Quart server initialization ##
app = Quart(__name__)

app.secret_key = os.getenv('APP_SECRET_KEY')
app.config["SECRET_KEY"] = os.getenv('APP_SECRET_KEY')
app.config["JWT_SECRET_KEY"] = os.getenv('JWT_TOKEN_KEY')
app.config["SESSION_COOKIE_DOMAIN"] = ".droptracker.io"
jwt = JWTManager(app)

# Add near the top where other app configurations are
app.config['PREFERRED_URL_SCHEME'] = 'https'
app.config['PROXY_FIX_X_FOR'] = 1
app.config['PROXY_FIX_X_PROTO'] = 1
app.config['PROXY_FIX_X_HOST'] = 1
app.config['PROXY_FIX_X_PREFIX'] = 1

@listen(Startup)
async def on_startup(event: Startup):
    global start_time
    start_time = time.time()
    global total_guilds
    print(f"Connected as {bot.user.display_name} with id {bot.user.id}")
    bot_ready.value = True
    bot.send_command_tracebacks = False
    await bot.change_presence(status=interactions.Status.ONLINE,
                              activity=interactions.Activity(name=f" /help", type=interactions.ActivityType.WATCHING))
    bot.load_extension("commands")
    bot.load_extension("tickets")
    print("Set bot to ready")
    await create_tasks()

@app.before_serving
async def ensure_http_1():
    pass

@app.before_request
async def ensure_no_protocol_switch():
    if request:
        if request.scheme == 'websocket':
            abort(400, "WebSockets are not supported")
        

## Guild Events ##

@listen(GuildJoin)
async def joined_guild(event: GuildJoin):
    global total_guilds
    total_guilds += 1
    guild = session.query(Guild).filter(Guild.guild_id == event.guild_id).first()
    if not guild:
        guild = Guild(guild_id=str(event.guild_id),
                            date_added=datetime.now())
        session.add(guild)
        session.commit()
    pass

@listen(GuildLeft)
async def left_guild(event: GuildLeft):
    global total_guilds
    total_guilds -= 1
    # guild = session.query(Guild).filter(Guild.guild_id == event.guild_id).first()
    # if guild:
    #     if guild.initialized and guild.group_id:
    #         group = session.query(Group).filter(Group.group_id == guild.group_id).first()
    #         group_settings = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == guild.group_id).all()
    #         group_associations = session.query(Group)
    pass

## Message Events ##

webhook_channels = []
last_webhook_refresh = datetime.now() - timedelta(hours=24)
ignored_list = [] ## TODO - store this better
last_xf_transfer = datetime.now() - timedelta(seconds=10)

@listen(MessageCreate)
async def on_message_create(event: MessageCreate):
    global last_xf_transfer
    global webhook_channels
    global last_webhook_refresh
    global ignored_list
    bot: interactions.Client = event.bot
    if bot.is_closed:
        await bot.astart(token=bot_token)
    await bot.wait_until_ready()
    message = event.message
    if message.author.system:  # or message.author.bot:
        return
    if message.author.id == bot.user.id:
        return
    if message.channel.type == ChannelType.DM or message.channel.type == ChannelType.GROUP_DM:
        return
    

    channel_id = message.channel.id
    

    # if str(message.channel.id) == "1262137292315688991":
    if datetime.now() - last_webhook_refresh > timedelta(hours=1):
        # Assuming webhook_channels is a list of extracted webhook IDs
        webhook_channels.clear()

        # Query your database to get the webhook URLs
        webhooks = session.query(Webhook.webhook_url).all()

        # Regex pattern to capture the webhook ID from the URL
        webhook_id_pattern = r'/webhooks/(\d+)/'

        # Loop through each webhook URL from the database
        webhook_list = []
        for webhook in webhooks:
            webhook_list.append(webhook[0])
        # webhook_list.append("https://discord.com/api/webhooks/1262137322741305374/m5KX8QTRhYck4Orbqqcwpe3240pZdZb9sfKAeLeuEzE0z-WVtuwSuuBhHacLy_lsNxth")
        for url in webhook_list:
            match = re.search(webhook_id_pattern, url)  # Search for the ID pattern in the URL
            if match:
                webhook_id = match.group(1)  # Extract the matched webhook ID
                webhook_channels.append(int(webhook_id))  # Add the webhook ID to the list

        # Update last webhook refresh time after processing
        last_webhook_refresh = datetime.now()
    if not message.webhook_id:
        return
    if int(message.webhook_id) in webhook_channels:
    #if str(channel_id) == "1262137292315688991":
        item_name = ""
        player_name = ""
        item_id = 0
        npc_name = "none"
        value = 0
        quantity = 0
        sheet_id = ""
        source_type = ""
        imageUrl = ""
        token = ""
        account_hash = ""
        for embed in message.embeds:
            field_names = [field.name for field in embed.fields]

            if "type" in field_names:
                field_values = [field.value.lower().strip() for field in embed.fields]
                rsn = ""
                if "collection_log" in field_values:
                    reported_slots = 1
                    for field in embed.fields:
                        if field.name == "item":
                            item_name = field.value
                        if field.name == "auth_key":
                            token = field.value
                        elif field.name == "player":
                            rsn = field.value
                        elif field.name == "item_id":
                            itemId = field.value
                        elif field.name == "source":
                            npcName = field.value
                        elif field.name == "acc_hash":
                            account_hash = field.value
                        elif field.name == "slots":
                            #print("Slots field:", field.value)
                            max_slots, reported_slots = field.value.split("/")
                            reported_slots = reported_slots.replace("/","")
                            max_slots = max_slots.replace("/","")
                            #print("reported, max slots:", reported_slots, "/",max_slots)
                        elif field.name == "rarity":
                            if field.value != "OptionalDouble.empty":
                                rarity = field.value
                            else:
                                rarity = ""
                        elif field.name == "sheet":
                            sheet_id = field.value
                        elif field.name == "kc":
                            if field.value != "null":
                                kc = field.value
                            else:
                                kc = 0

                    imageUrl = ""
                    if rsn == "":
                        return
                    attachment_url = None
                    attachment_type = None
                    if message.attachments:
                        for attachment in message.attachments:
                            if attachment.url:
                                attachment_url = attachment.url
                                attachment_type = attachment.content_type
                    await clog_processor(bot,
                                         player_name=rsn,
                                         account_hash=account_hash,
                                         auth_key=token,
                                         item_name=item_name,
                                         source=npcName,
                                         kc=kc,
                                         reported_slots=reported_slots,
                                         attachment_url=attachment_url,
                                         attachment_type=attachment_type)
                    continue
                elif "combat_achievement" in field_values:
                    if embed.fields:
                        acc_hash, task_type, points_awarded, points_total, completed_tier, auth_key = None, None, None, None, None, None
                        task_tier = None
                        for field in embed.fields:
                            if field.name == "acc_hash":
                                acc_hash = field.value
                            elif field.name == "points":
                                points_awarded = field.value
                            elif field.name == "total_points":
                                points_total = field.value
                            elif field.name == "completed":
                                completed_tier = field.value
                            elif field.name == "auth_key":
                                auth_key = field.value
                            elif field.name == "task":
                                task_name = field.value
                            elif field.name == "tier":
                                task_tier = field.value
                        attachment_url = None
                        attachment_type = None
                        if message.attachments:
                            for attachment in message.attachments:
                                if attachment.url:
                                    attachment_url = attachment.url
                                    attachment_type = attachment.content_type
                        await ca_processor(bot,
                                        acc_hash,
                                        auth_key,
                                        task_name,
                                        task_tier,
                                        points_awarded,
                                        points_total,
                                        completed_tier,
                                        attachment_url,
                                        attachment_type)
                elif "npc_kill" in field_values or "kill_time" in field_values:
                    npc_name = ""
                    current_time = ""
                    personal_best = ""
                    account_hash = ""
                    team_size = "Solo"
                    # print("npc_kill detected")
                    if embed.fields:
                        for field in embed.fields:
                            if field.name == "boss_name":
                                npc_name = field.value
                            elif field.name == "player_name":
                                player_name = field.value
                            if field.name == "auth_key":
                                token = field.value
                            elif field.name == "acc_hash":
                                account_hash = field.value
                            elif field.name == "kill_time":
                                current_time = field.value
                                current_time_ms = convert_to_ms(current_time)
                            elif field.name == "best_time":
                                personal_best = field.value
                                personal_best_ms = convert_to_ms(personal_best)
                            elif field.name == "is_pb":
                                is_new_pb = False if field.value == "false" else True 
                                if is_new_pb:
                                    ## A new PB sends no "pb", but instead a true boolean defining if the current_time is a new pb.
                                    personal_best_ms = current_time_ms
                            elif field.name == "Team_Size":
                                team_size = field.value
                            attachment_url = None
                            attachment_type = None
                            if message.attachments:
                                for attachment in message.attachments:
                                    if attachment.url:
                                        attachment_url = attachment.url
                                        attachment_type = attachment.content_type
                        #print("Sending to pb_processor")
                        await pb_processor(bot, 
                                            player_name, 
                                            account_hash,
                                            token,
                                            npc_name, 
                                            current_time_ms, 
                                            personal_best_ms, 
                                            team_size,
                                            is_new_pb,
                                            attachment_url, 
                                            attachment_type)
                elif embed.title and "received some drops" in embed.title or "drop" in field_values:
                    if embed.fields:
                        for field in embed.fields:
                            if field.name == "player":
                                player_name = field.value.strip()
                            elif field.name == "item":
                                item_name = field.value.strip()
                            elif field.name == "acc_hash":
                                account_hash = field.value.strip()
                            elif field.name == "id":
                                item_id = int(field.value.strip())
                            if field.name == "auth_key":
                                token = field.value
                            elif field.name == "source":
                                npc_name = field.value.strip()
                                if npc_name in ignored_list:
                                    return
                            elif field.name == "value":
                                if field.value:
                                    value = int(field.value)
                                else:
                                    value = 0
                            elif field.name == "quantity":
                                if field.value:
                                    quantity = int(field.value)
                                else:
                                    quantity = 1
                            elif field.name == "sheet_id" or field.name == "sheet":
                                sheet_id = field.value
                            elif field.name == "webhook" and len(field.value) > 10:
                                pass
                        attachment_url = None
                        attachment_type = None
                        if message.attachments:
                            for attachment in message.attachments:
                                if attachment.url:
                                    attachment_url = attachment.url
                                    attachment_type = attachment.content_type
                        drop_data = {"npc_name": npc_name,
                                     'item_name': item_name,
                                     'account_hash': account_hash,
                                     'auth_key': token,
                                     'value': value,
                                     'quantity': quantity,
                                     'player_name': player_name,
                                     'item_id': item_id,
                                     'attachment_url': attachment_url,
                                     'attachment_type': attachment_type}
                        # print("Sending drop data:", drop_data)
                        await drop_processor(bot, drop_data)
                        
                        continue

@app.errorhandler(Exception)
async def handle_exception(e):
    # await logger.log("error", f"Unhandled exception: {str(e)}", "/api/-based handle_exception")
    return jsonify(error=str(e)), 500


@Task.create(IntervalTrigger(minutes=10))
async def update_loot_leaderboards():
    print("Updating loot leaderboards...")
    await logger.log("access", "Beginning loot leaderboard update cycle...", "update_loot_leaderboards")
    all_groups = session.query(Group).all()
    groups_to_update = {}
    for group in all_groups:
        group_id = group.group_id
        configured_channel = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                     GroupConfiguration.config_key == 'lootboard_channel_id').first()
        configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                     GroupConfiguration.config_key == 'lootboard_message_id').first()
        if configured_channel and configured_message:
            if configured_channel.config_value:
                groups_to_update[group_id] = {"wom_id": group.wom_id,
                                            "channel": configured_channel.config_value,
                                            "message": configured_message.config_value}
    #print("Groups to update:", groups_to_update)
    for group_id, group in groups_to_update.items():
        try:
            channel: interactions.Channel = await bot.fetch_channel(channel_id=group['channel'])
            if channel:
                messages = await channel.fetch_messages(limit=15)
            else:
                ## TODO -- add logging; channel not found
                continue
        except Exception as e:
            print("Unable to fetch message history from channel id ", group['channel'], "e:", e)
            continue
        message_to_update = None
        group_obj = session.query(Group).filter(Group.group_id == group_id).first()
        if group['message'] != '':
            try:
                message = await channel.fetch_message(message_id=group['message'])
            except Exception as e:
                print("Couldn't fetch the message for this lootboard...:", e)
                await logger.log("error", f"The message for group {group_id}'s lootboard couldn't be fetched: {e}", "update_loot_leaderboards")
                staffchat = await bot.fetch_channel(channel_id=1210765308239945729)
                configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                        GroupConfiguration.config_key == 'lootboard_message_id').first()
                
                continue
                
        else:
            message_to_update = None
            for message in messages:
                if message.author.id == bot.user.id and message.embeds:
                    message_to_update = message
                    if message_to_update is not None and message.id != message_to_update.id:
                        try:
                            await message.delete()
                        except Exception as e:
                            await logger.log("error", f"Couldn't delete an old message for group {group_id}: {e}", "update_loot_leaderboards")
            if not message_to_update:
                await logger.log("error", f"No message ID found for group {group_id} ({group_obj.group_name}). We would have sent a new one right now...", "update_loot_leaderboards")
                try:
                    new_board = await channel.send(f"This loot leaderboard is being initialized.... Please wait a few moments.")
                    new_board_msg_id = new_board.id
                    configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'lootboard_message_id').first()
                    configured_message.config_value = str(new_board_msg_id)
                    session.commit()
                except Exception as e:
                    await logger.log("error", f"Couldn't send a message to the channel: {e}", "update_loot_leaderboards")
                staffchat = await bot.fetch_channel(channel_id=1210765308239945729)

                group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                        GroupConfiguration.config_key == 'lootboard_message_id').first()
                
            else: ## found previous message from the bot
                message = message_to_update
                configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'lootboard_message_id').first()
                if configured_message.config_value != str(message.id):  
                    configured_message.config_value = str(message.id)
                    session.commit()
                if not message:
                    message = await channel.send(f"<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                    await logger.log("error", f"No message ID found for group {group_id} ({group_obj.group_name}). Creating a new one...", "update_loot_leaderboards")
                    try:
                        configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                GroupConfiguration.config_key == 'lootboard_message_id').first()
                        configured_message.config_value = str(message.id)
                        session.commit()
                    except Exception as e:
                        await logger.log("error", f"Couldn't update the lootboard message ID with a new one... e: {e}", "update_loot_leaderboards")
        if not message:
            await logger.log("error", f"Couldn't get the message to update the loot leaderboard with...", "update_loot_leaderboards")
            try:
                message = await channel.send(f"<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                            GroupConfiguration.config_key == 'lootboard_message_id').first()
                configured_message.config_value = str(message.id)
                session.commit()
            except Exception as e:
                await logger.log("error", f"Couldn't send a new message to the channel: {e}", "update_loot_leaderboards")
            continue
        try: 
            wom_id = group['wom_id']
            if not wom_id:
                wom_id = 0
            image_path = await generate_server_board(bot, group_id=group_id, wom_group_id=wom_id)
            embed_template = await db.get_group_embed('lb', group_id)
            if group_id != 2:
                player_wom_ids = await fetch_group_members(wom_id)
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
            message.attachments.clear()
            lootboard = interactions.File(image_path)
            await message.edit(content="",embed=embed,files=lootboard)
        except Exception as e:
            await logger.log("error", f"Loot leaderboards -- Couldn't create/send {group_obj.group_name} (#{group_id})'s embed: {e}", "update_loot_leaderboards")
            print("Exception occured while updating the loot leaderboard for group", group_obj.group_name, "e:", e, "type:", type(e))
        # await logger.log("access", "Waiting 1 second before the next group board...", "update_loot_leaderboards")
        await asyncio.sleep(1)



@Task.create(IntervalTrigger(minutes=60))
async def start_group_sync():
    
    await update_group_members(bot)
    #await logger.log("access", "update_group_members completed...", "start_group_sync")


async def create_tasks():
    print("Starting the database sync tasks for Redis caching")
    await start_background_redis_tasks()
    ## Update the Cloudflare DNS resolution if necessary
    try:
        updater = CloudflareIPUpdater()
        asyncio.create_task(updater.start_monitoring(interval_seconds=300))
    except Exception as e:
        print("Couldn't start the Cloudflare IP updater:", e)


    await update_loot_leaderboards()
    update_loot_leaderboards.start()
    print("[Startup] Loot Leaderboard update task started/completed.")

    await update_pb_embeds()
    update_pb_embeds.start()

    print("Attempting to perform channel name updates...")
    await update_channel_names()
    update_channel_names.start()
    

    # Initialize the GitHubPagesUpdater
    updater = GithubPagesUpdater()
    
    print("Syncing group member association tables...")
    await start_group_sync()
    start_group_sync.start()
    print("Syncing patreon ranks...")
    patreon_sync.start()             # Patreon sync task
    await check_patreon_notifications()
    check_patreon_notifications.start()
    print("Scheduling GitHub updates...")
    updater.schedule_updates.start(updater) # GitHub updater task, scheduling every 60 minutes (only changing the file if actual changes are detected.)
    await logger.log("access", "Startup tasks completed.", "create_tasks")


@Task.create(IntervalTrigger(minutes=1))
async def check_patreon_notifications():
    new_notifications = session.execute(text("SELECT * FROM patreon_notification WHERE status = 0")).all()
    for notification in new_notifications:
        user_id = notification.user_id
        tier = notification.tier
        discord_id = notification.discord_id
        components = []
        try:
            user = await bot.fetch_user(user_id=discord_id)
            if user:
                patreon_sub_embed = Embed(title=f"Patreon Subscription Received!",
                                          description=f"You are now a tier `{tier}` patreon supporter of the DropTracker project!",
                                          color=0x00ff00)
                patreon_sub_embed.add_field(name="Thank you for your support!", value=f"Our project would not be able to exist without generous contributions from community members like yourself.")
                if tier >= 2:
                    patreon_sub_embed.add_field(name=f"Your subscription tier provides you Premium group access!",
                                                value=f"Please select which group you want to provide premium features to below:")
                    player_group_id_query = """SELECT group_id FROM user_group_association WHERE user_id = :user_id"""
                    player_group_ids = session.execute(text(player_group_id_query), {"user_id": user_id}).fetchall()
                    group_ids = [g[0] for g in player_group_ids]
                    groups = session.query(Group).filter(Group.group_id.in_(group_ids)).all()
                    for group in groups:
                        components.append(Button(style=ButtonStyle.PRIMARY, label=group.group_name, custom_id=f"patreon_group_{group.group_id}"))
                patreon_sub_embed.set_thumbnail(url=f"https://www.droptracker.io/img/droptracker-small.gif")
                patreon_sub_embed.set_footer(text="Powered by the DropTracker | https://www.droptracker.io/")
                await user.send(f"Hey, <@{discord_id}>!", embeds=[patreon_sub_embed], components=components)
                session.query(text("UPDATE patreon_notification SET status = 1 WHERE status = 0")).execute()
                session.commit()
        except Exception as e:
            print("Couldn't send a message to the user:", e)


@listen(Component)
async def on_component(event: Component):
    ctx = event.ctx
    custom_id = ctx.custom_id
    if custom_id.startswith("patreon_group_"):
        group_id = int(custom_id.split("_")[2])
        valid_patreon = session.query(GroupPatreon).filter(GroupPatreon.user_id == ctx.user.id).first()
        if valid_patreon and valid_patreon.group_id == None:
            valid_patreon.group_id = group_id
            group = session.query(Group).filter(Group.group_id == group_id).first()
            await ctx.send(f"You have assigned your DropTracker Patreon subscription perks to {group.group_name}!")
            try:
                await ctx.message.delete()
            except Exception as e:
                print("Couldn't delete the message:", e)
            return
        else:
            await ctx.send("You don't have a valid Patreon subscription, or you already have a group assigned to it.")
            return

@Task.create(IntervalTrigger(minutes=30))
async def update_pb_embeds():
    await logger.log("access", "update_pb_embeds called", "update_pb_embeds")
    
    enabled_hofs = session.query(GroupConfiguration).filter(
        GroupConfiguration.config_key == 'create_pb_embeds',
        GroupConfiguration.config_value == '1'
    ).all()
    
    for enabled_group in enabled_hofs:
        group: Group = enabled_group.group
        print("Group with enabled HOFs is named", group.group_name)
        
        enabled_bosses_raw = session.query(GroupConfiguration).filter(
            GroupConfiguration.group_id == group.group_id,
            GroupConfiguration.config_key == 'personal_best_embed_boss_list'
        ).first()
        
        if not enabled_bosses_raw:
            continue
            
        if enabled_bosses_raw.long_value == "" or enabled_bosses_raw.long_value == None:
            enabled_bosses = json.loads(enabled_bosses_raw.config_value)
        else:
            enabled_bosses = json.loads(enabled_bosses_raw.long_value)

        max_entries = session.query(GroupConfiguration.config_value).filter(
            GroupConfiguration.group_id == group.group_id,
            GroupConfiguration.config_key == 'number_of_pbs_to_display'
        ).first()
        
        max_entries = int(max_entries.config_value) if max_entries else 3
        
        channel_id = session.query(GroupConfiguration.config_value).filter(
            GroupConfiguration.group_id == group.group_id,
            GroupConfiguration.config_key == 'channel_id_to_send_pb_embeds'
        ).first()
        
        if not channel_id:
            continue
            
        channel_id = channel_id.config_value
        
        try:
            channel = await bot.fetch_channel(channel_id=int(channel_id), force=True)
            if not channel:
                continue
                
            
            # Fetch existing messages to identify which NPCs are already posted
            npcs = {}
            for boss in enabled_bosses:
                npc_id = session.query(NpcList.npc_id).filter(NpcList.npc_name == boss).first()
                if npc_id:
                    npcs[npc_id[0]] = boss
                    
            to_be_posted = set(npcs.keys())
            print("Processing", len(npcs), "NPCs for", group.group_name + "'s hall of fame...")
            # Process existing messages with proper rate limiting
            for i, npc_id in enumerate(list(npcs.keys())):
                existing_message = session.query(GroupPersonalBestMessage).filter(
                    GroupPersonalBestMessage.group_id == group.group_id, 
                    GroupPersonalBestMessage.boss_name == npcs[npc_id]
                ).first()
                
                if existing_message:
                    # Wait 6 seconds after every 4 messages
                    if i > 0 and i % 4 == 0:
                        await asyncio.sleep(7)
                    
                    try:
                        success, wait_for_rate_limit = await update_boss_pb_embed(bot, group.group_id, npc_id, from_submission=False)
                        if success:
                            # Only remove from to_be_posted if the update was successful
                            if npc_id in to_be_posted:
                                to_be_posted.remove(npc_id)
                        else:
                            pass
                        
                        # Small delay between each edit
                        if wait_for_rate_limit:
                            await asyncio.sleep(2)
                        
                    except Exception as e:
                        await asyncio.sleep(7)
            
            # Wait before posting new messages
            await asyncio.sleep(6)
            
            # Post new messages with proper rate limiting
            
            for i, npc_id in enumerate(to_be_posted):
                # Wait 6 seconds after every 4 messages
                if i > 0 and i % 4 == 0:
                    await asyncio.sleep(7)
                
                try:
                    pb_embed = await create_boss_pb_embed(group.group_id, npcs[npc_id], max_entries)
                    next_update = datetime.now() + timedelta(minutes=30)
                    future_timestamp = int(time.mktime(next_update.timetuple()))
                    now = datetime.now()
                    now_timestamp = int(time.mktime(now.timetuple()))
                    pb_embed.add_field(name="Next refresh:", value=f"<t:{future_timestamp}:R> (last: <t:{now_timestamp}:R>)", inline=False)
                    posted_message = await channel.send(embed=pb_embed)
                    
                    new_entry = GroupPersonalBestMessage(
                        group_id=group.group_id, 
                        boss_name=npcs[npc_id], 
                        message_id=posted_message.id, 
                        channel_id=channel.id
                    )
                    session.add(new_entry)
                    session.commit()
                    
                    # Small delay between each post
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    print(f"Error posting message for {npcs[npc_id]}: {e}")
                    await asyncio.sleep(7)
                    
        except Exception as e:
            print(f"Error processing channel {channel_id} for group {group.group_name}: {e}")
            
        # Wait between processing different groups
        await asyncio.sleep(7)


@Task.create(IntervalTrigger(minutes=30))
async def update_channel_names():
    loot_channel_id_configs = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_monthly_loot').all()
    #print("Got all loot channel id configs", loot_channel_id_configs)
    for channel_setting in loot_channel_id_configs:
        #print("Channel setting is:", channel_setting)
        if channel_setting.config_value != "":
            #print("Channel setting value is not empty")
            try:
                channel = await bot.fetch_channel(channel_id=channel_setting.config_value)
                if channel:
                    #print("Channel is not None")
                    if channel.type == ChannelType.GUILD_VOICE:
                        #print("Channel is a voice channel")
                        template = session.query(GroupConfiguration).filter(GroupConfiguration.config_key == 'vc_to_display_monthly_loot_text',
                                                                            GroupConfiguration.group_id == channel_setting.group_id).first()
                        template_str = template.config_value
                        if template_str == "" or not template_str:
                            template_str = "{month}: {gp_amount} gp"
                        if channel_setting.group_id != 2:
                            group_wom_id = session.query(Group.wom_id).filter(Group.group_id == channel_setting.group_id).first()
                            if group_wom_id:
                                group_wom_id = group_wom_id[0]
                            if group_wom_id:
                                #print("Finding group members?")
                                try:
                                    wom_member_list = await fetch_group_members(wom_group_id=int(group_wom_id))
                                except Exception as e:
                                    #print("Couldn't get the member list", e)
                                    return
                            player_ids = await associate_player_ids(wom_member_list)
                            clan_player_ids = wom_member_list if wom_member_list else []
                        else:
                            clan_player_ids = session.query(Player.player_id).all()
                            player_ids = [player_id[0] for player_id in clan_player_ids]
                        player_id = player_ids[0]
                        group_total = 0
                        from datetime import datetime
                        partition = datetime.now().year * 100 + datetime.now().month
                        for player_id in player_ids:
                            player_total_month = f"player:{player_id}:{partition}:total_loot"
                            player_month_total = redis_client.get(player_total_month)
                            if player_month_total is None:
                                player_month_total = 0
                            group_total += int(player_month_total)
                        month_str = datetime.now().strftime("%B")
                        #group_rank, ranked_in_group, group_total_month = calculate_clan_overall_rank(player_id, player_ids)
                        #group_total_month = format_number(group_total_month)
                        fin_text = template_str.replace("{month}", month_str).replace("{gp_amount}", format_number(group_total))
                        await channel.edit(name=f"{fin_text}")
                else:
                    print("Channel is not found for group ID", channel_setting.group_id, "and config value", channel_setting.config_value)
            except Exception as e:
                print("Couldn't edit the channel. e:", e)


async def run_discord_bot():
    async with aiohttp.ClientSession() as session:
        await bot.astart(bot_token)

front = create_frontend(bot)
api_bp = create_api(bot)
#admin_cp_bp = create_admin_cp(bot)
app.register_blueprint(api_bp, url_prefix='/api')
app.register_blueprint(front)

async def run_bot():
    while True:
        try:
            await bot.astart(bot_token)
        except Exception as e:
            await asyncio.sleep(5)  # Wait a bit before attempting to reconnect

async def main():
    while True:  # Continuous restart loop
        bot_task = asyncio.create_task(run_bot())
        hypercorn_config = create_hypercorn_config()
        quart_task = asyncio.create_task(hypercorn.asyncio.serve(app, hypercorn_config))
        
        try:
            await asyncio.gather(bot_task, quart_task)
        except Exception as e:
            print(f"An error occurred: {e}")
            # Properly clean up tasks
            for task in [bot_task, quart_task]:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            
            # Wait before attempting restart
            await asyncio.sleep(5)
            print("Restarting tasks...")
            continue  # Restart the loop
        
        # If we get here, tasks completed normally
        break


if __name__ == "__main__":
    asyncio.run(main())
