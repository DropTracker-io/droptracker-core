import random
import threading
from typing import List
import aiohttp
from interactions.client.errors import Forbidden, InteractionMissingAccess
from db.clan_sync import insert_xf_group
from db.entitlements import has_custom_embeds
from h11 import LocalProtocolError
import interactions
import json
from dotenv import load_dotenv
import asyncio
import os
import time
import multiprocessing
import signal
import sys
from monitor.sdnotifier import SystemdWatchdog

from sqlalchemy import text
from services.notification_service import NotificationService
from services.bot_state import BotState
from services.channel_names import ChannelNames
from services.channel_cache import shape_channel_cache
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
from interactions import Intents, Message, user_context_menu, ContextMenuContext, Member, listen, Status, Task, IntervalTrigger, \
    ActivityType, ChannelType, slash_command, Embed, slash_option, OptionType, check, is_owner, \
    slash_default_member_permission, Permissions, SlashContext, ButtonStyle, Button, SlashCommand, ComponentContext, \
    component_callback, Modal, ShortText, BaseContext, Extension, GuildChannel, ScheduledEventType, \
    ScheduledEventStatus
from interactions.api.events import GuildJoin, GuildLeft, MessageCreate, Component, Startup
from lootboard.generator import generate_server_board, get_generated_board_path
from utils.cloudflare_update import CloudflareIPUpdater
from utils.msg_logger import HighThroughputLogger
from utils.wiseoldman import fetch_group_members
from web.front import create_frontend
from commands import UserCommands, ClanCommands
from db.models import Event, EventGuild, Group, GroupConfiguration, GroupPatreon, GroupPersonalBestMessage, Guild, PersonalBestEntry, PlayerPet, Session, User, WebhookPendingDeletion, session, NpcList, ItemList, Webhook, Player

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

from utils.sentry import init_sentry
init_sentry("droptracker-core")

next_sync_time = datetime.now() + timedelta(minutes=5)

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

bot = interactions.Client(intents=Intents.DIRECT_MESSAGES | Intents.GUILD_INTEGRATIONS,
                          send_command_traceback=False,
                          owner_ids=[528746710042804247, 232236164776460288])
bot.send_not_ready_messages = True
bot.send_command_tracebacks = False

# --- Preserve the Discord Activity "Launch" entry-point command across syncs ---
# When Activities is enabled on this application, Discord auto-creates a type-4
# PRIMARY_ENTRY_POINT command (the Activity launcher). interactions.py builds
# its command-sync payload only from bot-defined commands and PUT-overwrites the
# whole global command list, so any deploy that changes a slash command would
# drop that launcher (a plain restart with no command changes doesn't sync, so
# it survives those). We wrap the payload builder to re-append the existing
# remote entry-point command whenever a sync would otherwise fire. This only
# preserves what Discord already created — it never invents a command — and is
# a no-op until Activities is enabled.
_ENTRY_POINT_CMD_TYPE = 4  # ApplicationCommandType.PRIMARY_ENTRY_POINT
_orig_build_sync_payload = bot._build_sync_payload


def _build_sync_payload_keep_entry_point(remote_commands, cmd_scope, local_cmds_json, delete_cmds):
    payload, needed = _orig_build_sync_payload(
        remote_commands, cmd_scope, local_cmds_json, delete_cmds
    )
    # Only a firing sync (needed) rewrites the command list; the up-to-date
    # no-op path leaves the remote entry point untouched already.
    if needed:
        try:
            already = any(int(c.get("type", 1)) == _ENTRY_POINT_CMD_TYPE for c in payload)
            if not already:
                for rc in remote_commands:
                    if int(rc.get("type", 1)) == _ENTRY_POINT_CMD_TYPE:
                        payload.append({
                            k: v for k, v in rc.items()
                            if k not in ("id", "application_id", "version")
                        })
                        break
        except Exception:
            discord_logger.exception("Failed to preserve Activity entry-point command during sync")
    return payload, needed


bot._build_sync_payload = _build_sync_payload_keep_entry_point

if os.getenv("STATUS") == "dev" or os.getenv("STATE") == "dev":
    bot_token = os.getenv('DEV_TOKEN')
else:
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
watchdog = None
shutdown_event = asyncio.Event()

# Health check functions for systemd watchdog
async def health_check():
    """Comprehensive health check for the application"""
    try:
        # Check if bot is ready and connected
        if not bot.is_ready:
            return False
        
        # Check if notification service is running
        if notification_service is None:
            return False
        if hasattr(notification_service, "is_running"):
            if not notification_service.is_running():
                return False
        
        # Check if Quart app is running (basic check)
        if app is None:
            return False
        
        return True
    except Exception as e:
        print(f"Health check failed: {e}")
        return False

# Signal handlers for graceful shutdown
def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_event.set()

def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown"""
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)

@listen(Startup)
async def on_startup(event: Startup):
    global app_logger
    global start_time
    start_time = time.time()
    global total_guilds
    global notification_service
    notification_service = NotificationService(bot, db)
    await notification_service.start()
    # Ensure the service actually started
    if hasattr(notification_service, "is_running") and not notification_service.is_running():
        # Attempt one more time in case the loop wasn't ready
        await notification_service.start()
    print(f"Connected as {bot.user.display_name} with id {bot.user.id}")
    bot_ready.value = True
    bot.send_command_tracebacks = False
    app_logger = AppLogger()
    await bot.change_presence(status=interactions.Status.ONLINE,
                              activity=interactions.Activity(name=f" /help", type=interactions.ActivityType.WATCHING))
    #bot.load_extension("services.update_dmer")
    bot.load_extension("commands")
    bot.load_extension("services.bot_state")
    bot.load_extension("services.message_handler")
    bot.load_extension("services.event_signup_discord")
    bot.load_extension("services.channel_names")
    bot.load_extension("services.components")
    bot.load_extension("services.entry_modifier")
    bot.load_extension("services.user_context")
    bot.load_extension("services.group_poll")
    bot.load_extension("services.activity_launch")
    print("Loaded services.")
    print("Set bot to ready")
    await asyncio.sleep(1)
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

def should_group_sync():
    last_sync = redis_client.get("last_group_sync")
    if not last_sync:
        # First time running, allow sync and set timestamp
        redis_client.set("last_group_sync", datetime.now().isoformat())
        return True
    
    last_sync = datetime.fromisoformat(last_sync)
    # Check if it's been over an hour since last sync
    if datetime.now() - last_sync > timedelta(hours=1):
        # Update timestamp before returning True to prevent multiple syncs
        redis_client.set("last_group_sync", datetime.now().isoformat())
        return True
    else:
        return False

async def update_group_members_task_channel():
    channel_id = 1489188732602024027
    channel = await bot.fetch_channel(channel_id=channel_id)
    global next_sync_time
    if channel:
        time_left = (next_sync_time - datetime.now()).total_seconds() / 60
        if time_left < 0:
            await channel.edit(name=f"Next WOM Refresh: soon")
        else:
            await channel.edit(name=f"Next WOM Refresh: ~{time_left:.0f}min")

@Task.create(IntervalTrigger(minutes=60))
async def start_group_sync():
    if should_group_sync():
        await update_group_members_task_channel()
        await update_group_members(bot)
    global next_sync_time
    next_sync_time = datetime.now() + timedelta(minutes=70)
    #await logger.log("access", "update_group_members completed...", "start_group_sync")


@Task.create(IntervalTrigger(minutes=3))
async def update_group_members_task_channel_loop():
    await update_group_members_task_channel()

@Task.create(IntervalTrigger(minutes=2))
async def event_board_updates():
    """Live event standings boards (services/event_board.py): the interval
    catch-all that keeps every active event's leaderboard-channel message
    fresh (time-remaining drift, missed hook refreshes) and renders the
    final state for just-ended events. Score-change refreshes also happen
    inline after each event notification sends."""
    try:
        from services.event_board import run_board_sweep
        await run_board_sweep(bot)
    except Exception as e:
        print(f"Event board sweep error: {e}")


@Task.create(IntervalTrigger(minutes=8))
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
                should_repost = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                            GroupConfiguration.config_key == 'repost_lootboard').first()
                if configured_channel and configured_message:
                    if configured_channel.config_value:
                        groups_to_update[group_id] = {"wom_id": group.wom_id,
                                                    "channel": configured_channel.config_value,
                                                    "message": configured_message.config_value,
                                                    "repost": should_repost.config_value}
            
            for group_id, group in groups_to_update.items():
                try:
                    channel: interactions.Channel = await bot.fetch_channel(channel_id=group['channel'])
                    if not channel:
                        #print(f"Channel with id {group['channel']} not found on discord for group {group_id} ({group_obj.group_name}).")
                        continue
                    message_to_update = None
                    group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                    
                    # Check if we should repost (create new message) or edit existing
                    should_repost_value = group['repost'] if group['repost'] else "false"
                    repost_enabled = should_repost_value.lower() in ['true', '1', 'yes', 'on']
                    
                    if repost_enabled:
                        # Check if there's an existing message to delete first
                        if group['message'] and group['message'] != '' and group['message'] != "0" and group['message'] != 0:
                            try:
                                old_message = await channel.fetch_message(message_id=group['message'])
                                await old_message.delete()
                                print(f"Deleted previous lootboard message for group {group_id} ({group_obj.group_name})")
                            except Exception as e:
                                print(f"Couldn't delete previous message for group {group_id} ({group_obj.group_name}): {e}")
                                # Continue anyway, we'll try to post a new message
                        
                        # Always create a new message when repost is enabled
                        try:
                            message = await channel.send(f"<a:loading:1180923500836421715> Please wait while we initialize this Loot Leaderboard....")
                            configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                        GroupConfiguration.config_key == 'lootboard_message_id').first()
                            configured_message.config_value = str(message.id)
                            session.commit()
                            print(f"Posted new lootboard message for group {group_id} ({group_obj.group_name}) with ID: {message.id}")
                        except Exception as e:
                            print(f"Couldn't send a new message to the channel: {e}")
                            continue
                    else:
                        # Use existing logic to find and edit existing message
                        if group['message'] != '' and group['message'] != "0" and group['message'] != 0:
                            try:
                                message = await channel.fetch_message(message_id=group['message'])
                            except Exception as e:
                                #print("Couldn't fetch the message for this lootboard...:", e)
                                continue
                                
                        else:
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
                            #staffchat = await bot.fetch_channel(channel_id=1210765308239945729)

                            group_obj = session.query(Group).filter(Group.group_id == group_id).first()
                            configured_message = session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id,
                                                                                    GroupConfiguration.config_key == 'lootboard_message_id').first()
                            
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
                        # Custom lootboard embeds are a subscription perk; everyone
                        # else gets the template group's default embed.
                        lb_embed_group = group_id if has_custom_embeds(group_id) else 1
                        embed_template = await db.get_group_embed('lb', lb_embed_group)
                    except Exception as e:
                        print("Unable to obtain embed_template for group", group_obj.group_name, "e:", e)
                        continue
                    if group_id != 2:
                        total_tracked = group_obj.get_player_count()
                    else:
                        total_tracked = session.query(Player.wom_id).count()
                    next_update = datetime.now() + timedelta(seconds=615)
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
                        #print("Updated the loot leaderboard for group", group_obj.group_name)
                    except Exception as e:
                        print("Unable to edit the message for group", group_obj.group_name, "e:", e)
                        continue
                except InteractionMissingAccess or Forbidden:
                    print(f"A forbidden/interaction access error occurred sending a lootboard. Performing custom logic!")
                    from services.notification_service import get_authorized_users
                    guild_admins: List[interactions.User] = await get_authorized_users(group_id)
                    for admin in guild_admins:

                        try:
                            await admin.send(f"Hey, {admin.mention}!\n" + 
                            f"It looks like the <@{bot.user.id}> bot does not have permissions for your `Loot Leaderboard` channel!\n" + 
                            f"Double check that you've provided us **Read Message History** and **Manage Messages** permissions!")
                        except Exception as e:
                            print("Couldn't DM a server admin about a failed lootboard update: " + e)
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
    

async def create_tasks():
    # Interval tasks first — they only *schedule* here (first fire is one
    # interval away), so nothing below can delay them. These used to start
    # after the slow awaited warm-ups (guild-cache sweep over every guild +
    # lootboards + WOM sync — many minutes), which left e.g. web_event_guilds
    # rows stuck 'pending' for the whole warm-up after every restart.
    drain_channel_cache_requests.start()
    print("Starting heartbeat monitoring...")
    heartbeat_check.start()
    notification_force.start()
    drain_discord_outbox.start()
    activity_launch_cards.start()
    reconcile_event_scheduled_events.start()
    event_board_updates.start()
    badge_cycle.start()
    # Lootboard POSTING is user-visible and must stay with the interval tasks
    # above — NEVER gated behind the multi-minute, rate-limited guild-cache /
    # WOM warm-ups below. It previously sat after `await cache_guild_channels()`
    # + `await cache_bot_guilds()`; when that guild sweep (228 DB guilds, many
    # 404, heavy 429 backoff) ballooned to ~25min per startup, restart churn
    # meant `.start()` was never reached and every group's board went stale for
    # hours. Schedule it now; it fires on its own 8-min interval.
    print("Starting lootboards")
    lootboard_updates.start()
    # Kick an immediate first pass WITHOUT blocking the rest of startup (the old
    # blocking `await lootboard_updates()` here is exactly what let the guild
    # warm-ups below starve posting). Fire-and-forget; the task body has its own
    # top-level try/except so it can never surface as an unhandled task error.
    asyncio.create_task(lootboard_updates())
    # Cheap and independent — run this first among the warm-ups so the Web
    # API's channel picker has data within seconds of startup instead of
    # waiting behind the slow WOM-sync pass below.
    await cache_guild_channels()
    cache_guild_channels.start()
    # Guild list for the event Discord config (Task 19) — same cheap REST
    # pattern; run once at startup then every 5 minutes.
    await cache_bot_guilds()
    cache_bot_guilds.start()
    print("Syncing group member association tables...")
    await start_group_sync()
    start_group_sync.start()
    update_group_members_task_channel_loop.start()
    await logger.log("access", "Startup tasks completed.", "create_tasks")


@Task.create(IntervalTrigger(minutes=60))
async def badge_cycle():
    """Evaluate automatic badges (services/badges.py). The engine keeps a
    Redis day marker, so all but the first run after daily rollover are a
    single Redis GET; on downtime it catches up the missed days itself."""
    try:
        from services.badges import run_badge_cycle
        stats = await asyncio.to_thread(run_badge_cycle)
        if stats.get("days"):
            print(f"Badge cycle processed {stats['days']}: "
                  f"daily={stats['daily']} streaks={stats['streaks']} records={stats['records']}")
    except Exception as e:
        print(f"Badge cycle failed: {e}")


@Task.create(IntervalTrigger(seconds=10))
async def drain_discord_outbox():
    """Drain the Web API's Discord outbox (announcement syndication + admin
    message sender). Additive; failures are isolated per row."""
    try:
        from services.discord_outbox import drain_once
        from db.models import Session as _Session
        await drain_once(bot, _Session)
    except Exception as e:
        print(f"Couldn't drain discord outbox: {e}")


@Task.create(IntervalTrigger(seconds=60))
async def activity_launch_cards():
    """Keep each group's standing "Open DropTracker" card in sync with its
    activity_launch_channel config: post it, move it when the channel changes,
    remove it when cleared. Cheap in steady state (DB reads only unless a card
    actually needs posting/moving/deleting)."""
    try:
        from services.activity_launch import reconcile_all
        await reconcile_all(bot, Session)
    except Exception as e:
        print(f"Couldn't reconcile activity launch cards: {e}")


@Task.create(IntervalTrigger(seconds=30))
async def reconcile_event_scheduled_events():
    """Mirror web_event_guilds desired state onto real Discord scheduled
    events (services/event_scheduled_events.py). The Web API only writes
    desired rows; this task is the only place that talks to Discord: it
    creates rows marked `pending` (once the event has a valid future start),
    edits rows that already carry a `discord_scheduled_event_id` — never
    re-creates one, so repeated edits can't spawn duplicates — and deletes
    `delete_pending` rows. A future start only gates *creation*: existing
    scheduled events keep receiving name/description (and still-future end)
    edits after the event has started. Failures are isolated per row;
    `failed` rows are not re-polled (no retry storm on a permanent Forbidden
    — the next event edit flips them back to `pending`)."""
    from services.event_scheduled_events import (
        DESCRIPTION_MAX,
        NAME_MAX,
        event_created_ping,
        future_end,
        sched_fields,
        schedulable,
    )
    from services.event_notifications import load_event_channels

    db_session = Session()
    try:
        rows = (
            db_session.query(EventGuild)
            .filter(EventGuild.sync_status.in_(("pending", "delete_pending")))
            .limit(50)
            .all()
        )
        for row in rows:
            try:
                if row.sync_status == "delete_pending":
                    if row.discord_scheduled_event_id:
                        guild = await bot.fetch_guild(row.guild_id)
                        # No guild (bot kicked) -> nothing left to clean up on
                        # Discord's side; just drop the row. force=True skips
                        # the library cache — a stale copy of an already-
                        # deleted event would 404 the delete below.
                        se = (
                            await guild.fetch_scheduled_event(
                                row.discord_scheduled_event_id, force=True
                            )
                            if guild else None
                        )
                        if se:
                            await se.delete(reason="DropTracker event ended")
                    db_session.delete(row)
                    db_session.commit()
                    continue

                ev = db_session.query(Event).filter(Event.id == row.event_id).first()
                if not ev:
                    continue
                # A future start is only required to CREATE a scheduled event
                # (Discord rejects past starts). Rows that already carry one
                # must keep syncing edits after the start passes — the old
                # gate here skipped every pend-flipped row once starts_at was
                # no longer in the future, so mid-event name/time edits
                # applied on the website but never reached Discord.
                creatable = schedulable(ev)
                if not row.discord_scheduled_event_id and not creatable:
                    continue  # stays pending until it has a valid future start
                guild = await bot.fetch_guild(row.guild_id)
                if not guild:
                    raise RuntimeError("bot is not a member of this guild")
                if row.discord_scheduled_event_id:
                    # force=True: bypass the library cache so a Discord-side
                    # deletion is actually seen (a stale cached object would
                    # 404 the edit and fail the row instead).
                    se = await guild.fetch_scheduled_event(
                        row.discord_scheduled_event_id, force=True
                    )
                    if se and se.status not in (
                        ScheduledEventStatus.SCHEDULED, ScheduledEventStatus.ACTIVE
                    ):
                        se = None  # completed/cancelled on Discord — nothing editable
                    if not se:
                        # Gone on Discord's side — forget the id. With a
                        # future start the next tick re-creates it; without
                        # one it can't come back (Discord rejects past
                        # starts), so surface that instead of silently
                        # re-skipping the row forever.
                        row.discord_scheduled_event_id = None
                        if not creatable:
                            row.sync_status = "failed"
                            row.last_error = (
                                "The Discord scheduled event no longer exists and "
                                "the start time has passed — it cannot be re-created"
                            )
                        db_session.commit()
                        continue
                    if creatable and se.status == ScheduledEventStatus.SCHEDULED:
                        start, end, _location = sched_fields(ev)
                        await se.edit(
                            name=ev.name[:NAME_MAX],
                            start_time=start,
                            end_time=end,
                            description=(ev.description or "")[:DESCRIPTION_MAX] or None,
                        )
                    else:
                        # Already-started (ACTIVE) or past-start events:
                        # Discord rejects start_time changes there, but name/
                        # description — and an end moved to a still-future
                        # time — must keep syncing.
                        edit_kwargs = {
                            "name": ev.name[:NAME_MAX],
                            "description": (ev.description or "")[:DESCRIPTION_MAX] or None,
                        }
                        new_end = future_end(ev)
                        if new_end is not None:
                            edit_kwargs["end_time"] = new_end
                        await se.edit(**edit_kwargs)
                else:
                    start, end, location = sched_fields(ev)
                    se = await guild.create_scheduled_event(
                        name=ev.name[:NAME_MAX],
                        event_type=ScheduledEventType.EXTERNAL,
                        start_time=start,
                        end_time=end,
                        external_location=location,
                        description=(ev.description or "")[:DESCRIPTION_MAX] or None,
                        reason="DropTracker event",
                    )
                    row.discord_scheduled_event_id = str(se.id)
                    # Companion ping: scheduled events can't mention roles
                    # themselves, so announce the fresh event in the
                    # configured announcements channel with the configured
                    # role pings (ping_config['event_created']; primary
                    # guild only). Never lets a ping failure fail the sync.
                    try:
                        ping_channel_id, ping_content = event_created_ping(
                            ev, row.guild_id, row.discord_scheduled_event_id,
                            load_event_channels(db_session, ev.id),
                        )
                        if ping_channel_id and ping_content:
                            ping_channel = await bot.fetch_channel(channel_id=ping_channel_id)
                            if ping_channel:
                                # Deep-link buttons: members can open the event
                                # inside the Discord Activity or on the website
                                # right from the creation announcement — the
                                # event page ships pre-activation for members
                                # of participating groups, so these links work
                                # even while the event is still a draft.
                                from services.activity_launch_core import (
                                    activity_link_url,
                                    launch_button_custom_id,
                                )
                                from services.event_message_layouts import deeplink_enabled

                                if deeplink_enabled():
                                    open_btn = interactions.Button(
                                        style=interactions.ButtonStyle.BLURPLE,
                                        label="Open in Discord",
                                        custom_id=launch_button_custom_id(ev.id),
                                    )
                                else:
                                    open_btn = interactions.Button(
                                        style=interactions.ButtonStyle.LINK,
                                        label="Open in Discord",
                                        url=activity_link_url(ev.id),
                                    )
                                await ping_channel.send(
                                    content=ping_content,
                                    components=[interactions.ActionRow(
                                        open_btn,
                                        interactions.Button(
                                            style=interactions.ButtonStyle.LINK,
                                            label="Event page",
                                            url=f"https://www.droptracker.io/events/{ev.id}",
                                        ),
                                    )],
                                    allowed_mentions=interactions.AllowedMentions(parse=["roles"]),
                                )
                    except Exception as ping_err:
                        print(f"[sched-event] ping failed for event {ev.id}: {ping_err}")
                row.sync_status = "synced"
                row.synced_at = datetime.now()
                row.last_error = None
                db_session.commit()
            except Exception as e:
                db_session.rollback()
                try:
                    row.sync_status = "failed"
                    row.last_error = str(e)[:255]
                    db_session.commit()
                except Exception:
                    db_session.rollback()
                print(f"[sched-event] guild={row.guild_id} event={row.event_id}: {e}")
    except Exception as e:
        print(f"Couldn't reconcile scheduled events: {e}")
    finally:
        db_session.close()

async def cache_channels_for_guild(guild_id) -> bool:
    """Fetch one guild's text + forum channels (and the forums' active
    threads) via REST and cache them to Redis (`guild:{id}:channels`). Works
    for *any* guild the bot is a member of — not just group home guilds — so
    events can target dedicated event servers (Task 19). Threads let groups
    route notifications into forum posts instead of separate channels
    (suggestion #3). Also caches the guild's roles (`guild:{id}:roles`) for
    the announcement ping picker. Returns True when the cache was written."""
    try:
        guild = await bot.fetch_guild(guild_id)
        if not guild:
            return False
        raw_channels = await guild.fetch_channels()
        try:
            active_threads = (await guild.fetch_active_threads()).threads
        except Exception as e:
            print(f"Couldn't fetch active threads for guild {guild_id}: {e}")
            active_threads = []
        channels = shape_channel_cache(raw_channels, active_threads)
        redis_client.setex(f"guild:{guild_id}:channels", 600, json.dumps(channels))
        # Cache the guild's roles for the announcement ping picker.
        # NOTE: `guild.fetch_roles()` is a discord.py method; it does not exist in
        # interactions.py (this project's library). We don't need it: the
        # `bot.fetch_guild()` call above already populated the role cache from the
        # REST guild payload (GET /guilds/{id} embeds `roles`), so the `guild.roles`
        # property is available here without the GUILDS gateway intent.
        try:
            roles = sorted(
                (
                    {"id": str(r.id), "name": r.name, "position": r.position or 0}
                    for r in guild.roles
                    # Skip @everyone (its id == guild id; picked via a
                    # dedicated toggle) and bot-managed integration roles.
                    if str(r.id) != str(guild_id) and not getattr(r, "managed", False)
                ),
                key=lambda r: -r["position"],
            )
            redis_client.setex(f"guild:{guild_id}:roles", 600, json.dumps(roles))
        except Exception as e:
            print(f"Couldn't cache roles for guild {guild_id}: {e}")
        return True
    except Exception as e:
        print(f"Couldn't cache channels for guild {guild_id}: {e}")
        return False


@Task.create(IntervalTrigger(minutes=5))
async def cache_guild_channels():
    """Cache guild channel lists (text, forums, active threads) to Redis
    (`guild:{id}:channels`) so the
    Web API's Discord channel pickers (group config UI + event Discord config)
    can list them without the Web API ever holding a bot token / Discord
    connection itself — it only reads this cache.

    Uses REST (`bot.fetch_guild` + `guild.fetch_channels()`), not the gateway's
    passive `bot.guilds` cache: this bot doesn't run the `GUILDS` intent, so
    that cache never populates. Covers guilds with a linked ``Group`` row plus
    any guild targeted by a web event (``web_events.discord_guild_id`` — which
    may be a dedicated event server that is nobody's home guild). 10-minute
    Redis TTL as a staleness guard if the bot goes down; this task refreshes
    it every 5 minutes while running.
    """
    try:
        session = Session()
        guild_ids = {
            str(g[0])
            for g in session.query(Group.guild_id).filter(Group.guild_id != None).distinct().all()  # noqa: E711
        }
        guild_ids |= {
            str(g[0])
            for g in session.query(Event.discord_guild_id).filter(Event.discord_guild_id != None).distinct().all()  # noqa: E711
        }
    except Exception as e:
        print(f"Couldn't load guild ids for channel cache: {e}")
        return

    cached = 0
    for guild_id in guild_ids:
        if await cache_channels_for_guild(guild_id):
            cached += 1
    print(f"Cached channel lists for {cached}/{len(guild_ids)} guilds.")


_DISCORD_API_BASE = "https://discord.com/api/v10"


async def _fetch_bot_guilds_rest():
    """Enumerate every guild the bot is in via Discord REST
    (`GET /users/@me/guilds`, paginated), honouring 429 / Retry-After.

    We deliberately do NOT use `bot.http.get_guilds()`: in interactions.py 5.16
    that call routes through a shared per-bucket rate limiter whose
    `lock_for_duration` raises ``RuntimeError('Attempted to lock a bucket that
    is already locked.')`` when this low-limit endpoint is paginated. That was
    firing on every sweep, so `bot:guilds` never populated and the events
    Discord config UI showed "the bot's server list isn't cached yet". Nor can
    we read `bot.guilds`: without the GUILDS intent the gateway cache stays
    empty. So we page the REST endpoint ourselves and back off on 429 — the
    concern that motivated the previous library call (the older hand-rolled
    request gave up on the first 429) is handled here by honouring Retry-After.
    """
    token = getattr(getattr(bot, "http", None), "token", None) or bot_token
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DropTracker (https://www.droptracker.io, 1.0)",
    }
    guilds = []
    after = None
    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            params = {"limit": 200}
            if after:
                params["after"] = after
            page = None
            # Bounded per-page retry so one 429 / transient 5xx backs off and
            # retries rather than aborting (and discarding) the whole sweep.
            for attempt in range(6):
                async with session.get(
                    f"{_DISCORD_API_BASE}/users/@me/guilds", params=params
                ) as resp:
                    if resp.status == 429:
                        try:
                            retry_after = float((await resp.json()).get("retry_after", 1.0))
                        except Exception:
                            retry_after = float(resp.headers.get("Retry-After", 1.0) or 1.0)
                        await asyncio.sleep(min(retry_after, 60) + 0.5)
                        continue
                    if resp.status in (500, 502, 503, 504):
                        await asyncio.sleep(1 + attempt * 2)
                        continue
                    resp.raise_for_status()
                    page = await resp.json()
                    break
            else:
                raise RuntimeError("exhausted retries fetching /users/@me/guilds")

            if not isinstance(page, list) or not page:
                break
            guilds.extend(
                {"id": str(g.get("id")), "name": g.get("name"), "icon": g.get("icon")}
                for g in page if g.get("id")
            )
            if len(page) < 200:
                break
            after = str(page[-1].get("id"))
            # Gentle spacing so a large-guild bot doesn't hammer this low-limit
            # endpoint (still trivial next to the 5-minute sweep interval).
            await asyncio.sleep(0.5)
    return guilds


@Task.create(IntervalTrigger(minutes=5))
async def cache_bot_guilds():
    """Cache every guild the bot is a member of to Redis (`bot:guilds`) as a
    JSON list of {id, name, icon} for the Web API's event Discord config
    (Task 19: an event can target any guild the bot is in). 15-minute TTL as a
    staleness guard; refreshed every 5 minutes while the bot runs. Only the
    fully-enumerated list is written — a mid-pagination failure leaves the
    previous cache in place rather than publishing a partial list.
    """
    try:
        guilds = await _fetch_bot_guilds_rest()
        redis_client.setex("bot:guilds", 900, json.dumps(guilds))
        print(f"Cached bot:guilds ({len(guilds)} guilds).")
    except Exception as e:
        print(f"Couldn't refresh bot:guilds: {e}")


@Task.create(IntervalTrigger(seconds=15))
async def drain_channel_cache_requests():
    """Serve on-demand channel-cache requests from the Web API: when the event
    Discord config UI asks for a guild whose channels aren't cached yet, the
    Web API SADDs the guild id to `bot:channels:refresh` and we warm the cache
    here within seconds (instead of waiting for the 5-minute sweep)."""
    try:
        for _ in range(10):
            raw = redis_client.client.spop("bot:channels:refresh")
            if not raw:
                break
            guild_id = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            if guild_id.isdigit():
                await cache_channels_for_guild(guild_id)
    except Exception as e:
        print(f"Couldn't drain channel cache requests: {e}")

@Task.create(IntervalTrigger(seconds=30))
async def notification_force():
    try:
        await notification_service.force_process_notifications()
    except Exception as e:
        print(f"Couldn't force process the notification queue: {e}")





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
    # Ensure notification service loop is alive
    global notification_service
    if notification_service is not None and hasattr(notification_service, "is_running"):
        if not notification_service.is_running():
            try:
                await notification_service.start()
            except Exception as e:
                app_logger.log(log_type="error", data=f"Failed to restart notification service: {e}", app_name="main", description="heartbeat_check")

async def run_discord_bot():
    async with aiohttp.ClientSession() as session:
        await bot.astart(bot_token)

front = create_frontend(bot)
#admin_cp_bp = create_admin_cp(bot)
app.register_blueprint(front)

async def run_bot():
    while True:
        try:
            await bot.astart(bot_token)
        except Exception as e:
            await asyncio.sleep(5)  # Wait a bit before attempting to reconnect

async def main():
    global watchdog
    
    # Setup signal handlers
    setup_signal_handlers()
    
    # Initialize systemd watchdog
    watchdog = SystemdWatchdog()
    watchdog.set_health_check(health_check)
    
    try:
        async with watchdog:
            # Notify systemd that we're ready
            await watchdog.notify_ready()
            print("Systemd watchdog initialized and ready notification sent")
            
            while not shutdown_event.is_set():  # Check for shutdown signal
                bot_task = asyncio.create_task(run_bot())
                hypercorn_config = create_hypercorn_config()
                quart_task = asyncio.create_task(hypercorn.asyncio.serve(app, hypercorn_config))
                
                try:
                    # Wait for either tasks to complete or shutdown signal
                    done, pending = await asyncio.wait(
                        [bot_task, quart_task, asyncio.create_task(shutdown_event.wait())],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    # If shutdown was requested, cancel all tasks
                    if shutdown_event.is_set():
                        print("Shutdown requested, cancelling tasks...")
                        for task in [bot_task, quart_task]:
                            if not task.done():
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass
                        break
                    
                    # Check if any task failed
                    for task in done:
                        if task.exception():
                            print(f"Task failed: {task.exception()}")
                            # Cancel remaining tasks
                            for remaining_task in [bot_task, quart_task]:
                                if not remaining_task.done():
                                    remaining_task.cancel()
                                    try:
                                        await remaining_task
                                    except asyncio.CancelledError:
                                        pass
                            
                            # Wait before attempting restart (unless shutdown requested)
                            if not shutdown_event.is_set():
                                await asyncio.sleep(5)
                                print("Restarting tasks...")
                                continue
                            else:
                                break
                
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
                    
                    # Wait before attempting restart (unless shutdown requested)
                    if not shutdown_event.is_set():
                        await asyncio.sleep(5)
                        print("Restarting tasks...")
                        continue
                    else:
                        break
            
            print("Application shutting down gracefully...")
            # Stop notification service on shutdown
            try:
                if notification_service is not None:
                    await notification_service.stop()
            except Exception:
                pass
            
    except KeyboardInterrupt:
        print("Received keyboard interrupt")
    except Exception as e:
        print(f"Fatal error in main: {e}")
        raise
    finally:
        print("Cleanup completed")




if __name__ == "__main__":
    asyncio.run(main())