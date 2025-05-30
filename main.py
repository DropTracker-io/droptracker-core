import random
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
from db.update_player_total import background_task, start_background_redis_tasks
from services.notification_service import NotificationService
from services.bot_state import BotState
from services.lootboards import Lootboards
from services.channel_names import ChannelNames
from utils.ge_value import get_true_item_value
from utils.embeds import create_boss_pb_embed, update_boss_pb_embed
from utils.logger import LoggerClient
from db.app_logger import AppLogger

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
from interactions import GuildText, Intents, Message, user_context_menu, ContextMenuContext, Member, listen, Status, Task, IntervalTrigger, \
    ActivityType, ChannelType, slash_command, Embed, slash_option, OptionType, check, is_owner, \
    slash_default_member_permission, Permissions, SlashContext, ButtonStyle, Button, SlashCommand, ComponentContext, \
    component_callback, Modal, ShortText, BaseContext, Extension, GuildChannel
from interactions.api.events import GuildJoin, GuildLeft, MessageCreate, Component, Startup
from pb.leaderboards import create_pb_embeds
from lootboard.generator import generate_server_board, get_generated_board_path
from utils.cloudflare_update import CloudflareIPUpdater
from utils.msg_logger import HighThroughputLogger
from utils.wiseoldman import fetch_group_members
from web.api import create_api
from web.front import create_frontend
from commands import UserCommands, ClanCommands
from tickets import Tickets
from db.models import Group, GroupConfiguration, GroupPatreon, GroupPersonalBestMessage, Guild, PersonalBestEntry, PlayerPet, Session, User, WebhookPendingDeletion, session, NpcList, ItemList, Webhook, Player

from db.ops import associate_player_ids, update_group_members
from db.ops import DatabaseOperations
from utils.messages import message_processor, joined_guild_msg
from utils.patreon import patreon_sync
from utils.redis import RedisClient, calculate_clan_overall_rank
from utils.download import download_player_image
from utils.github import GithubPagesUpdater
from data.submissions import ca_processor, drop_processor, pb_processor, clog_processor
from utils.format import get_sorted_doc_files, format_time_since_update, format_number, get_command_id, get_extension_from_content_type, convert_to_ms, get_true_boss_name, replace_placeholders
from datetime import datetime, timedelta
import logging
from games.gielinor_race.routes import gielinor_race_bp

bot_ready = Value('b', False)  # 'b' is for boolean
logger = LoggerClient(token=os.getenv('LOGGER_TOKEN'))
discord_logger = logging.getLogger('interactions')
logging.basicConfig(level=logging.DEBUG)
#discord_logger.setLevel(logging.DEBUG)

# Create a custom filter for Discord's 404 errors
class Discord404Filter(logging.Filter):
    def filter(self, record):
        if "404" in record.getMessage() and any(x in record.getMessage() for x in ["/channels/", "/messages/"]):
            return False
        return True
discord_logger.addFilter(Discord404Filter())
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
bot.send_command_tracebacks = False

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

notification_service = None

@listen(Startup)
async def on_startup(event: Startup):
    global app_logger
    global start_time
    start_time = time.time()
    global total_guilds
    global notification_service
    notification_service = NotificationService(bot, db)
    print(f"Connected as {bot.user.display_name} with id {bot.user.id}")
    bot_ready.value = True
    bot.send_command_tracebacks = False
    app_logger = AppLogger()
    await bot.change_presence(status=interactions.Status.ONLINE,
                              activity=interactions.Activity(name=f" /help", type=interactions.ActivityType.WATCHING))
    bot.load_extension("services.bot_state")
    bot.load_extension("services.message_handler")
    bot.load_extension("services.channel_names")
    bot.load_extension("services.components")
    bot.load_extension("services.hall_of_fame")
    bot.load_extension("commands")
    bot.load_extension("tickets")
    print("Loaded services.")
    print("Set bot to ready")
    await create_tasks()


## Quart server functions ##

@app.before_serving
async def ensure_http_1():
    pass

@app.before_request
async def ensure_no_protocol_switch():
    if request:
        if request.scheme == 'websocket':
            abort(400, "WebSockets are not supported")
        


## Message Events ##

webhook_channels = []
last_webhook_refresh = datetime.now() - timedelta(days=400)
ignored_list = [] ## TODO - store this better
last_xf_transfer = datetime.now() - timedelta(seconds=10)
message_data_logger = HighThroughputLogger("/store/droptracker/disc/data/logs/msg_tracker.json")



@app.errorhandler(Exception)
async def handle_exception(e):
    # await logger.log("error", f"Unhandled exception: {str(e)}", "/api/-based handle_exception")
    return jsonify(error=str(e)), 500



@Task.create(IntervalTrigger(minutes=60))
async def start_group_sync():
    await update_group_members(bot)
    #await logger.log("access", "update_group_members completed...", "start_group_sync")


@Task.create(IntervalTrigger(minutes=10))
async def lootboard_updates():
    try:
        print("Updating loot leaderboards...")
        session = None
        try:
            session = Session()
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
            
            for group_id, group in groups_to_update.items():
                try:
                    channel: interactions.Channel = await bot.fetch_channel(channel_id=group['channel'])
                    message_to_update = None
                    group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                    if group['message'] != '':
                        try:
                            message = await channel.fetch_message(message_id=group['message'])
                        except Exception as e:
                            #print("Couldn't fetch the message for this lootboard...:", e)
                            continue
                            
                    else:
                        try:
                            messages = await channel.fetch_messages(limit=15)
                        except Exception as e:
                            # print("Unable to fetch message history from channel id ", group['channel'], "e:", e)
                            continue
                        message_to_update = None
                        for message in messages:
                            if message.author.id == bot.user.id and message.embeds:
                                message_to_update = message
                                if message_to_update is not None and message.id != message_to_update.id:
                                    try:
                                        await message.delete()
                                    except Exception as e:
                                        print(f"Couldn't delete an old message for group {group_id}: {e}")
                        if not message_to_update:
                            print(f"No message ID found for group {group_id} ({group_obj.group_name}). We would have sent a new one right now...")
                            try:
                                new_board = await channel.send(f"This loot leaderboard is being initialized.... Please wait a few moments.")
                                new_board_msg_id = new_board.id
                                configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                            GroupConfiguration.config_key == 'lootboard_message_id').first()
                                configured_message.config_value = str(new_board_msg_id)
                                session.commit()
                            except Exception as e:
                                print(f"Couldn't send a message to the channel: {e}")
                                continue
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
                                print(f"No message ID found for group {group_id} ({group_obj.group_name}). Creating a new one...")
                                try:
                                    configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                            GroupConfiguration.config_key == 'lootboard_message_id').first()
                                    configured_message.config_value = str(message.id)
                                    session.commit()
                                except Exception as e:
                                    print(f"Couldn't update the lootboard message ID with a new one... e: {e}")
                    if not message:
                        print(f"Couldn't get the message to update the loot leaderboard with...")
                        try:
                            message = await channel.send(f"<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                            configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                        GroupConfiguration.config_key == 'lootboard_message_id').first()
                            configured_message.config_value = str(message.id)
                            session.commit()
                        except Exception as e:
                            print(f"Couldn't send a new message to the channel: {e}")
                        continue
                    
                    wom_id = group['wom_id']
                    if not wom_id:
                        wom_id = 0
                        # Use the direct URL and call the updates in our external process.
                    image_path = f"/store/droptracker/disc/static/assets/img/clans/{group_id}/lb/lootboard.png"
                    if not os.path.exists(image_path):
                        print(f"Lootboard image not found for group {group_id} ({group_obj.group_name}).")
                        continue
                    
                    try:
                        embed_template = await db.get_group_embed('lb', group_id)
                    except Exception as e:
                        print("Unable to obtain embed_template for group", group_obj.group_name, "e:", e)
                        continue
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
                    try:
                        embed = replace_placeholders(embed_template, value_dict)
                    except Exception as e:
                        print("Unable to replace placeholders for group", group_obj.group_name, "e:", e)
                        continue
                    try:
                        message.attachments.clear()
                        lootboard = interactions.File(image_path)
                        await message.edit(content="",embed=embed,files=lootboard)
                        print("Updated the loot leaderboard for group", group_obj.group_name)
                    except Exception as e:
                        print("Unable to edit the message for group", group_obj.group_name, "e:", e)
                        continue
                except Exception as e:
                    configured_style = None
                    try:
                        configured_style = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                            GroupConfiguration.config_key == 'loot_board_type').first()
                    except:
                        pass
                        
                    if configured_style:
                        # app_logger.log(log_type="error", data=f"Loot leaderboards -- Couldn't create/send {group_obj.group_name} (#{group_id})'s embed: {e}\n" + 
                        #                  "Board style is:" + configured_style.config_value, app_name="core", description="update_loot_leaderboards")
                        print("Exception occurred while updating the loot leaderboard for group", group_obj.group_name, "e:", e, "type:", type(e))
                    else:
                        print("Exception occurred while updating the loot leaderboard for group", group_obj.group_name, "e:", e, "type:", type(e))
                # Wait 1 second before processing the next group
                await asyncio.sleep(1)
        except Exception as e:
            print(f"Major error in loot leaderboard update: {e}")
            if session:
                try:
                    session.rollback()
                except:
                    pass
        finally:
            if session:
                try:
                    session.close()
                except:
                    pass
        
        print("Completed loot leaderboard update. Waiting 5 minutes before the next update.")
    except Exception as e:
        print(f"Critical error in loot leaderboard update loop: {e}")
    
    # Wait 5 minutes before the next update
    await asyncio.sleep(300)

async def create_tasks():    
    notification_sync.start()
    print("Starting lootboards")
    await lootboard_updates()
    lootboard_updates.start()
    background_task.start()
    print("Syncing group member association tables...")
    await start_group_sync()
    start_group_sync.start()
    await logger.log("access", "Startup tasks completed.", "create_tasks")
    print("Starting heartbeat monitoring...")
    heartbeat_check.start()

@Task.create(IntervalTrigger(seconds=5))
async def notification_sync():
    await notification_service.process_pending_notifications()




@Task.create(IntervalTrigger(seconds=60))
async def heartbeat_check():
    """Check if the bot is still connected and reconnect if needed"""
    global bot
    
    if not bot.is_ready:
        app_logger.log(log_type="warning", data="Bot is not ready, attempting to reconnect", app_name="main", description="heartbeat_check")
        try:
            await bot.astart(bot_token)
        except Exception as e:
            app_logger.log(log_type="error", data=f"Failed to reconnect bot: {e}", app_name="main", description="heartbeat_check")

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
