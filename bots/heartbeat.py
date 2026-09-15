import asyncio
from datetime import datetime
import os
import random
import time
import signal
import sys
from dotenv import load_dotenv
import interactions
from interactions import GuildText, IntervalTrigger, Permissions, Task, listen, slash_command
from interactions.api.events import Startup, MessageCreate
import json
import logging
from typing import Dict, List
from db.models import BackupWebhook, Webhook, NewWebhook, Session, session, WebhookPendingDeletion
import sqlalchemy
from monitor.sdnotifier import SystemdWatchdog
from dotenv import load_dotenv
load_dotenv()

from utils.sentry import init_sentry
init_sentry("droptracker-heartbeat")
# Set up more detailed logging
# logging.basicConfig(level=logging.DEBUG)
import os


def _heartbeat_token():
    """The token for this instance, refusing to fall back to production's.

    Unlike bots/main.py this had no dev variant at all, so a dev box with
    HEARTBEAT_BOT_TOKEN populated (it shares production's .env) connected as the
    *production* heartbeat bot. Returning None makes an unconfigured dev
    instance fail to start rather than impersonate prod.
    """
    from utils.dev_guild_guard import is_dev_mode

    if is_dev_mode():
        return os.getenv("DEV_HEARTBEAT_BOT_TOKEN")
    return os.getenv("HEARTBEAT_BOT_TOKEN")


bot_token = _heartbeat_token()
bot = interactions.Client(token=bot_token)

# Global variables for systemd watchdog
watchdog = None
shutdown_event = asyncio.Event()

# interactions' Client can't be restarted in-process: login() re-gathers every
# module-level command, so a second astart() on the same client raises
# "Duplicate Command! 0::run_creation_loop" before it reaches Discord. The loop
# that used to retry here did exactly that, forever: the bot was dead
# 2026-09-07 -> 09-11 and again from 2026-09-15 14:16 while systemd showed it
# running. Now main() exits when the client stops or the gateway stays down for
# NOT_READY_EXIT_SECONDS, and systemd (Restart=always) starts a fresh process.
# SystemdWatchdog keeps pinging even when the health check fails, so that exit
# is what actually restarts the bot.
NOT_READY_UNHEALTHY_SECONDS = 120
NOT_READY_EXIT_SECONDS = 600
# Restarts are at least this far apart, so a hard failure can't burn through
# Discord's 1000-identifies-a-day limit (exceeding it resets the token).
MIN_RUN_SECONDS = 120

_last_ready_at = time.monotonic()  # process start counts, as the grace to connect


def not_ready_seconds(is_ready: bool, last_ready_at: float, now: float) -> tuple:
    """(seconds since the gateway was last ready, updated last-ready time)"""
    if is_ready:
        return 0.0, now
    return now - last_ready_at, last_ready_at


def _seconds_not_ready() -> float:
    global _last_ready_at
    seconds, _last_ready_at = not_ready_seconds(bot.is_ready, _last_ready_at, time.monotonic())
    return seconds


# Health check function for systemd watchdog
async def health_check():
    """Unhealthy once the gateway has been down for NOT_READY_UNHEALTHY_SECONDS."""
    try:
        return _seconds_not_ready() < NOT_READY_UNHEALTHY_SECONDS
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


async def shutdown_bot_client() -> None:
    """Attempt a clean client shutdown between restart attempts."""
    try:
        if hasattr(bot, "stop"):
            maybe_coro = bot.stop()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
            return
        if hasattr(bot, "close"):
            maybe_coro = bot.close()
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
    except asyncio.CancelledError:
        # interactions' Client.stop() awaits its own shard task, which is being
        # cancelled as part of the stop — so the CancelledError bubbling out here
        # is expected, not a failure. If we let it escape it propagates through
        # main() and asyncio.run(), and the process exits status=1/FAILURE
        # (systemd logs a failed unit + "Unclosed client session"). Swallow it so
        # a deliberate shutdown exits 0.
        print("Heartbeat bot shard task cancelled during shutdown (expected, exiting cleanly).")
    except Exception as e:
        print(f"Error while closing heartbeat bot client: {e}")


async def run_bot_client() -> None:
    """Run the Discord client once; returns or raises when it stops. Never
    retries on the same client (see the note above NOT_READY_EXIT_SECONDS)."""
    print("Starting heartbeat bot client...")
    await bot.astart(token=bot_token)


async def wait_until_disconnected_too_long() -> None:
    """Return once the gateway has been down for NOT_READY_EXIT_SECONDS."""
    while _seconds_not_ready() < NOT_READY_EXIT_SECONDS:
        await asyncio.sleep(30)

# Global webhook state storage
# Structure: {channel_id: {webhook_id: webhook_data}}
webhook_states: Dict[str, Dict[str, dict]] = {}

pending_bot_deletions = set()
recently_created_webhook_ids = set()

main_parent_ids = [1332506635775770624, 1332506742801694751, 1369779266945814569, 1369779329382482005, 1369803376598192128]
hooks_parent_ids = [1332506904840372237, 1332506935886348339, 1369779098246975638, 1369779125035991171]
hooks_2_parent_ids = [1369777536975900773, 1369777572577284167, 1369778911264641034, 1369778925919670432, 1369778911264641034]
hooks_3_parent_ids = [1369780179064590418, 1369780228930670705, 1369780244583547073, 1369780261000183848, 1369780569080332369]

all_parent_ids = main_parent_ids + hooks_parent_ids + hooks_2_parent_ids + hooks_3_parent_ids

load_dotenv()

joel_id = 528746710042804247

# --- Discord constraints and creation throttling ---
MAX_WEBHOOKS_PER_CHANNEL = 15
DESIRED_WEBHOOKS_PER_CHANNEL = 1
MAX_CREATIONS_PER_RUN = 5
CREATE_RETRY_DELAYS = [2, 5, 10]  # seconds

# Global rate limiting primitives
creation_semaphore = asyncio.Semaphore(1)
GLOBAL_CREATION_COOLDOWN_UNTIL: float = 0.0

# Channels that need at least one webhook created
channels_needing_webhook = set()


def get_webhook_type_for_parent(parent_id: int) -> str:
    webhook_type = "backup"
    if parent_id in main_parent_ids:
        webhook_type = "core"
    elif parent_id in hooks_parent_ids:
        webhook_type = "hooks"
    elif parent_id in hooks_2_parent_ids:
        webhook_type = "hooks-2"
    elif parent_id in hooks_3_parent_ids:
        webhook_type = "hooks-3"
    return webhook_type


def tracked_webhook_ids() -> set:
    """Ids of every webhook any webhook table knows.

    Match webhooks on id, never URL: Discord withholds the token of a webhook
    another application created, so interactions' Webhook.url is None for 996
    of the 999 webhooks in the main pool guild. Looking those up by URL is what
    queued the NULL-url pending rows whose channels got deleted."""
    with Session() as s:
        ids = set()
        for model in (Webhook, WebhookPendingDeletion, BackupWebhook):
            ids.update(str(webhook_id) for (webhook_id,) in s.query(model.webhook_id).all() if webhook_id)
        return ids


def untracked_webhooks(webhooks, tracked_ids) -> list:
    """The webhooks whose id no webhook table knows."""
    return [webhook for webhook in webhooks if str(webhook.id) not in tracked_ids]


async def safe_create_webhook_in_channel(channel: interactions.GuildText, name: str, avatar: interactions.File) -> interactions.Webhook | None:
    # Retry with simple backoff for transient rate-limit errors; stop immediately on capacity errors
    global GLOBAL_CREATION_COOLDOWN_UNTIL
    async with creation_semaphore:
        # Honor global cooldown window if set
        now = time.time()
        if GLOBAL_CREATION_COOLDOWN_UNTIL > now:
            await asyncio.sleep(GLOBAL_CREATION_COOLDOWN_UNTIL - now)
        for idx, delay in enumerate([0] + CREATE_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                webhook: interactions.Webhook = await channel.create_webhook(name=name, avatar=avatar)
                return webhook
            except Exception as e:
                message = str(e)
                # Capacity reached -> permanent for this channel until manual cleanup
                if "Maximum number of webhooks" in message or "Maximum number of webhooks reached" in message:
                    return None
                # On obvious rate-limit indicators, set short global cooldown and retry; otherwise, give up
                if "rate limit" in message.lower() or "429" in message:
                    GLOBAL_CREATION_COOLDOWN_UNTIL = time.time() + 2
                    continue
                return None
        return None

@slash_command(name="run_creation_loop", description="Run the creation loop manually",
               default_member_permissions=Permissions.ADMINISTRATOR)
async def run_creation_loop(ctx: interactions.SlashContext):
    if str(ctx.author.id) != str(joel_id):
        await ctx.send("You are not authorized to run this command.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)
    await run_new_webhook_loop()
    await ctx.send("Creation loop has been executed manually. 80 new webhooks have been added to the database.")

@Task.create(IntervalTrigger(minutes=480)) ## Runs every 8 hours to generate ~80 new webhooks
async def run_new_webhook_loop():
    print("Run_new_webhook_loop called.")
    """
    Rotates out all current webhooks, moves them to pending deletion, and generates new ones for both core and backup sets.
    """
    # Move all current webhooks to pending deletion
    all_webhooks = session.query(Webhook).all()
    pending_deletion = session.query(WebhookPendingDeletion).all()
    all_existing = all_webhooks + pending_deletion
    if len(all_existing) > 750:
        notification_channel = await bot.fetch_channel(1369649855194202223)
        print("We already have over 750 total webhooks, skipping this cycle...")
        await notification_channel.send(f"We already have over 750 total webhooks, skipping this cycle...")
        return
    for webhook in all_webhooks:
        for channel_id, webhooks_dict in webhook_states.items():
            for wh_id, wh_data in webhooks_dict.items():
                if wh_id == str(webhook.webhook_id):
                    # Store webhook and channel info in pending deletion
                    pending = WebhookPendingDeletion(
                        webhook_id=webhook.webhook_id,
                        webhook_url=webhook.webhook_url,
                        channel_id=channel_id,  # Store channel_id for later deletion
                        date_added=datetime.now(),
                        date_updated=datetime.now()
                    )
                    session.add(pending)
        session.delete(webhook)
    session.commit()

    # Generate new webhooks for core and backup sets
    print("Generating new set of core webhooks...")
    for i in range(40):
        new_core = await create_new_webhook(should_create=False) # Don't create a new webhook since we do that below
        if new_core is not None:
            session.add(Webhook(
                webhook_id=new_core["webhook_id"],
                webhook_url=new_core["webhook_url"],
                type="core"
            ))
        else:
            print("Failed to create a new core webhook in the loop.")
            i -= 1
        if i % 10 == 0:
            print("Sleeping for 10 seconds to prevent rate limiting...")
            await asyncio.sleep(10)

    print("Generating new set of backup webhooks...")
    for i in range(40):
        new_backup = await create_new_webhook(should_create=False)
        if new_backup is not None:
            session.add(Webhook(
                webhook_id=new_backup["webhook_id"],
                webhook_url=new_backup["webhook_url"],
                type="backup"
            ))
        else:
            print("Failed to create a new backup webhook in the loop.")
            i -= 1
        if i % 10 == 0:
            print("Sleeping for 10 seconds to prevent rate limiting...")
            await asyncio.sleep(10)

    print("New webhooks created, committing to database...")
    notification_channel = await bot.fetch_channel(1369649855194202223)
    await notification_channel.send(f"I just created `{len(all_webhooks)}` new webhooks, and marked `{len(all_webhooks)}` old webhooks as pending deletion.")
    session.commit()

async def load_initial_webhook_states():
    total_loaded = 0
    """Load all webhooks from all channels on startup"""
    tracked_ids = tracked_webhook_ids()
    for guild in bot.guilds:
        for channel in guild.channels:
            try:
                if channel.type == interactions.ChannelType.GUILD_CATEGORY:
                    if channel.channels and channel.id in all_parent_ids:
                        category_id = channel.id
                        for channel in channel.channels:
                            if isinstance(channel, GuildText):
                                # Initialize a new dictionary for this channel if it doesn't exist
                                if str(channel.id) not in webhook_states:
                                    webhook_states[str(channel.id)] = {}

                                webhooks = await channel.fetch_webhooks()
                                if len(webhooks) > 0:
                                    for webhook in webhooks:
                                        # Match on id (see tracked_webhook_ids). Adopt an untracked
                                        # webhook only when Discord gave us its URL.
                                        if str(webhook.id) not in tracked_ids and webhook.url:
                                            try:
                                                new_webhook = Webhook(
                                                    webhook_id=webhook.id,
                                                    webhook_url=webhook.url,
                                                    type=get_webhook_type_for_parent(category_id)
                                                )
                                                session.add(new_webhook)
                                                session.commit()
                                                tracked_ids.add(str(webhook.id))
                                            except Exception as e:
                                                print(f"Error adding webhook to database: {e}")
                                                session.rollback()
                                        ## Add it to the memory states of webhooks
                                        webhook_states[str(channel.id)][str(webhook.id)] = {
                                            'id': str(webhook.id),
                                            'name': webhook.name,
                                            'url': webhook.url if hasattr(webhook, 'url') else None,
                                            'token': webhook.token if hasattr(webhook, 'token') else None,
                                            'channel_id': str(channel.id),
                                            'guild_id': str(guild.id)
                                        }
                                # If channel has no webhooks, mark it as needing one
                                if len(webhooks) == 0:
                                    channels_needing_webhook.add(str(channel.id))
            except Exception as e:
                print(f"Error loading webhooks for channel {channel.id}: {str(e)}")
            finally:
                if total_loaded % 50 == 0:
                    print(f"Finished loading from #{total_loaded}")
                total_loaded += 1
    print(f"Loaded webhook states for {len(webhook_states)} channels")
    await bot.change_presence(status=interactions.Status.ONLINE,
                              activity=interactions.Activity(name=f" {len(webhook_states)}({int(len(webhook_states) / 3)}) webhooks", type=interactions.ActivityType.WATCHING))
    

@Task.create(IntervalTrigger(minutes=10))
async def check_missing_webhooks():
    """Count webhooks in the pool categories that no webhook table knows (by id).

    Report-only. It used to queue them into webhook_pending_deletion by URL, but
    the URL is None for webhooks another app created, so it queued a NULL-url
    row and the cleanup loop (removed 2026-09-15) deleted that row's whole
    channel 96h later: one pool channel every 4 days."""
    tracked_ids = tracked_webhook_ids()
    total_untracked = 0
    for guild in bot.guilds:
        for channel in guild.channels:
            if channel.type == interactions.ChannelType.GUILD_CATEGORY:
                if channel.id in all_parent_ids:
                    if channel.channels:
                        for channel in channel.channels:
                            if isinstance(channel, GuildText):
                                webhooks = await channel.fetch_webhooks()
                                total_untracked += len(untracked_webhooks(webhooks, tracked_ids))
    print(f"Pool categories hold {total_untracked} webhook(s) no webhook table tracks (left alone)")

## GitHub Pages publishing now has a single writer: the change-gated
## github_update_loop in data/player_total_updater.py. The 15-minute publisher
## that lived here raced it and (before change-gating) committed every cycle.


@Task.create(IntervalTrigger(minutes=30))
async def run_channel_deletes():#
    ## Actually acts as a webhook replacement loop instread of channel deletions
    global main_parent_ids, hooks_parent_ids, hooks_2_parent_ids, hooks_3_parent_ids
    parent_ids = main_parent_ids + hooks_parent_ids + hooks_2_parent_ids + hooks_3_parent_ids
    notification_channel = await bot.fetch_channel(1369649855194202223)
    created_this_run = 0
    for guild in bot.guilds:
        for channel in guild.channels:
            if isinstance(channel, GuildText):
                try:
                    if channel.parent_id:
                        if channel.parent_id in parent_ids and channel.type == interactions.ChannelType.GUILD_TEXT:
                            try:
                                channel: interactions.GuildText = channel
                                logo_path = '/store/droptracker/disc/static/assets/img/droptracker-small.gif'
                                avatar = interactions.File(logo_path)
                                # Skip if channel already at capacity or already has desired number
                                existing_hooks = await channel.fetch_webhooks()
                                if len(existing_hooks) >= MAX_WEBHOOKS_PER_CHANNEL or len(existing_hooks) >= DESIRED_WEBHOOKS_PER_CHANNEL:
                                    channels_needing_webhook.discard(str(channel.id))
                                    continue
                                # Global throttle per run to reduce rate-limit pressure
                                if created_this_run >= MAX_CREATIONS_PER_RUN:
                                    # brief backoff before scanning other channels
                                    await asyncio.sleep(1)
                                    continue
                    
                                # Determine a good webhook name based on channel name
                                webhook_name = f"DropTracker Webhooks ({channel.name.replace('drops-', '')})"
                                webhook = await safe_create_webhook_in_channel(channel, webhook_name, avatar)
                                if webhook is None:
                                    # Mark channel to retry later
                                    channels_needing_webhook.add(str(channel.id))
                                    continue
                                webhook_url = webhook.url
                                print("Created webhook, checking for duplicates in database...")
                                with session.no_autoflush:
                                    existing = session.query(Webhook).filter_by(webhook_url=webhook_url).first()
                                    if existing:
                                        print(f"Webhook URL {webhook_url} already exists in database, skipping insert.")
                                        return
                                    print("No duplicate found, adding to database...")
                                    # Determine webhook type based on channel's parent category
                                    webhook_type = get_webhook_type_for_parent(channel.parent_id)
                                        
                                    db_webhook = Webhook(webhook_id=webhook.id, webhook_url=webhook.url, type=webhook_type)
                                    session.add(db_webhook)
                                    session.commit()
                                    recently_created_webhook_ids.add(str(webhook.id))
                                    await notification_channel.send(f"Webhook replacement at url {webhook.url} created successfully in <#{channel.id}>")
                                    created_this_run += 1
                                    channels_needing_webhook.discard(str(channel.id))
                            except Exception as e:
                                print(f"Error creating new webhook: {e}")
                                await notification_channel.send(f"Error creating new webhook: {e}")
                                # Apply light backoff on error to be gentle on rate limits
                                await asyncio.sleep(1)
                            finally:
                                pending_changes.discard(channel.id)
                                
                except Exception as e:
                    print(f"Error deleting channel {channel.name}: {e}")


@listen(Startup)
async def on_startup():
    print("Bot starting up -- loading webhook states...")
    await check_missing_webhooks()
    # await run_new_webhook_loop()
    await load_initial_webhook_states()
    # #print("Checking for missing webhooks in the database based on the current webhook states...")
    # check_missing_webhooks.start()
    # await check_missing_webhooks()
    # Start ongoing maintenance tasks
    check_missing_webhooks.start()
    # Only attempt creations for channels we know need a webhook
    await run_channel_deletes()
    run_channel_deletes.start()
    # print("Returned from channel delete func")
    #run_new_webhook_loop.start()
    #await run_new_webhook_loop()

@listen("raw_webhooks_update")
async def on_raw_webhooks_update(event):
    try:
        if hasattr(event, 'data'):
            event_data = event.data
            if 'guild_id' in event_data and 'channel_id' in event_data:
                try:
                    channel = await bot.fetch_channel(event_data['channel_id'])
                    if channel:
                        print(f"Checking webhook changes based on raw_webhooks_update for channel {channel.name} in guild {channel.guild.name}")
                        await check_webhook_changes(channel)
                    else:
                        print("Channel not found")
                except Exception as e:
                    print(f"Error getting channel: {str(e)}")
            else:
                print(f"Raw webhook update data: {json.dumps(event_data, indent=2, default=str)}")
        else:
            pass
    except Exception as e:
        print(f"Error processing raw webhook update: {str(e)}")

pending_changes = set()

async def check_webhook_changes(channel: interactions.BaseChannel):
    channel_id = str(channel.id)
    # --- Ignore if this is a bot-initiated deletion ---
    if channel.id in pending_bot_deletions or channel.id in pending_changes:
        print(f"Ignoring webhook/channel deletion for {channel_id} (bot-initiated).")
        pending_bot_deletions.discard(channel.id)
        return
    notification_channel = await bot.fetch_channel(1369649855194202223)
    current_webhooks = await channel.fetch_webhooks()
    
    # Convert current webhooks to a comparable format
    current_webhook_data = {
        str(webhook.id): {
            'id': str(webhook.id),
            'name': webhook.name,
            'url': webhook.url if hasattr(webhook, 'url') else None,
            'token': webhook.token if hasattr(webhook, 'token') else None,
            'channel_id': channel_id,
            'guild_id': str(channel.guild.id)
        }
        for webhook in current_webhooks
    }
    
    # Get previous state for this channel
    previous_webhooks = webhook_states.get(channel_id, {})
    
    # Find new webhooks
    new_webhooks = {
        webhook_id: webhook_data 
        for webhook_id, webhook_data in current_webhook_data.items() 
        if webhook_id not in previous_webhooks and webhook_id not in recently_created_webhook_ids
    }
    
    # Find deleted webhooks
    deleted_webhooks = {
        webhook_id: webhook_data 
        for webhook_id, webhook_data in previous_webhooks.items() 
        if webhook_id not in current_webhook_data
    }
    
    # Handle new webhooks (no early return: the stored state below must update)
    if new_webhooks:
        print(f"New webhooks created in channel {channel.name}:")
        #notification_channel = await bot.fetch_channel(1369649855194202223)
        #await notification_channel.send(f"{len(new_webhooks)} new webhook creations were detected.")

    # Handle deleted webhooks
    if deleted_webhooks:
        print(f"Webhooks deleted from channel {channel.name}:")
        notification_channel = await bot.fetch_channel(1369649855194202223)
        for webhook_id, webhook_data in deleted_webhooks.items():
            print(f"  - Name: {webhook_data['name']}, ID: {webhook_id}")
            pending_changes.add(channel.id)
            # Delete from database, by id: webhook_data['url'] is None for
            # webhooks another app created (see tracked_webhook_ids).
            result = 0
            try:
                for model in (Webhook, BackupWebhook, WebhookPendingDeletion):
                    result = session.query(model).filter(model.webhook_id == webhook_id).delete()
                    if result:
                        break
                session.commit()
            except Exception as e:
                print(f"Error deleting webhook from database: {e}")
                session.rollback()

            # Send notification after all operations
            if result:
                await notification_channel.send(
                    f"I detected a webhook deletion; removing it from the database and creating a new one."
                )
            # Create new webhook in the same channel
            print("Webhook deleted, attempting to create a new one")
            if channel.type == interactions.ChannelType.GUILD_TEXT:
                try:
                    channel: interactions.GuildText = channel
                    logo_path = '/store/droptracker/disc/static/assets/img/droptracker-small.gif'
                    avatar = interactions.File(logo_path)
                    # Capacity check
                    existing_hooks = await channel.fetch_webhooks()
                    if len(existing_hooks) >= MAX_WEBHOOKS_PER_CHANNEL or len(existing_hooks) >= DESIRED_WEBHOOKS_PER_CHANNEL:
                        pending_changes.discard(channel.id)
                        channels_needing_webhook.discard(str(channel.id))
                        continue
                    # Determine a good webhook name based on channel name
                    webhook_name = f"DropTracker Webhooks ({channel.name.replace('drops-', '')})"
                    webhook = await safe_create_webhook_in_channel(channel, webhook_name, avatar)
                    if webhook is None:
                        pending_changes.discard(channel.id)
                        channels_needing_webhook.add(str(channel.id))
                        continue
                    webhook_url = webhook.url
                    print("Created webhook, checking for duplicates in database...")
                    with session.no_autoflush:
                        existing = session.query(Webhook).filter_by(webhook_url=webhook_url).first()
                        if existing:
                            print(f"Webhook URL {webhook_url} already exists in database, skipping insert.")
                            return
                        print("No duplicate found, adding to database...")
                        # Determine webhook type based on channel's parent category
                        webhook_type = get_webhook_type_for_parent(channel.parent_id)
                            
                        db_webhook = Webhook(webhook_id=webhook.id, webhook_url=webhook.url, type=webhook_type)
                        session.add(db_webhook)
                        session.commit()
                        recently_created_webhook_ids.add(str(webhook.id))
                        await notification_channel.send(f"Webhook replacement at url {webhook.url} created successfully in <#{channel.id}>")
                except Exception as e:
                    print(f"Error creating new webhook: {e}")
                    await notification_channel.send(f"Error creating new webhook: {e}")
                finally:
                    pending_changes.discard(channel.id)
    
    # Update stored state
    webhook_states[channel_id] = current_webhook_data
    # After processing, clear out old IDs to prevent memory leak
    recently_created_webhook_ids.difference_update(current_webhook_data.keys())

async def create_new_webhook(should_create=True):
    notification_channel = await bot.fetch_channel(1369649855194202223)
    servers = ["main", "hooks", "hooks-2", "hooks-3"]
    server = random.choice(servers)
    global main_parent_ids, hooks_parent_ids, hooks_2_parent_ids, hooks_3_parent_ids
    parent_ids = main_parent_ids + hooks_parent_ids + hooks_2_parent_ids + hooks_3_parent_ids
    try:
        if len(parent_ids) == 0:
            print("No parent IDs left to create new webhooks inside... exiting...")
            return
        parent_id = random.choice(parent_ids)
        print("Got parent ID, fetching parent channel...")
        parent_channel = await bot.fetch_channel(parent_id)
        current_channels = len(parent_channel.channels)
        if current_channels >= 49:
            print("Parent channel has 50 or more channels, skipping...")
            if parent_id in main_parent_ids:
                main_parent_ids.remove(parent_id)
            elif parent_id in hooks_parent_ids:
                hooks_parent_ids.remove(parent_id)
            elif parent_id in hooks_2_parent_ids:
                hooks_2_parent_ids.remove(parent_id)
            elif parent_id in hooks_3_parent_ids:
                hooks_3_parent_ids.remove(parent_id)
            parent_id = random.choice(parent_ids)
            parent_channel = await bot.fetch_channel(parent_id)
            if len(parent_channel.channels) >= 49:
                print("Second selected parent channel has 50 or more channels, skipping...")
                if parent_id in main_parent_ids:
                    main_parent_ids.append(parent_id)
                elif parent_id in hooks_parent_ids:
                    hooks_parent_ids.append(parent_id)
                elif parent_id in hooks_2_parent_ids:
                    hooks_2_parent_ids.append(parent_id)
                elif parent_id in hooks_3_parent_ids:
                    hooks_3_parent_ids.append(parent_id)
                return
        print("Fetched parent channel, creating new channel...")
        num = 0
        channel_name = f"drops-{num}"
        while channel_name in [channel.name for channel in parent_channel.channels]:
            num += 1
            channel_name = f"drops-{num}"
        new_channel: GuildText = await parent_channel.create_text_channel(channel_name)
        print("Created new channel, creating webhook...")
        logo_path = '/store/droptracker/disc/static/assets/img/droptracker-small.gif'
        avatar = interactions.File(logo_path)
        webhook: interactions.Webhook = await new_channel.create_webhook(name=f"DropTracker Webhooks ({num})", avatar=avatar)
        webhook_url = webhook.url
        print("Created webhook, checking for duplicates in database...")

        # Prevent autoflush when checking for duplicates
        if should_create:
            with session.no_autoflush:
                existing = session.query(Webhook).filter_by(webhook_url=webhook_url).first()
            if existing:
                print(f"Webhook URL {webhook_url} already exists in database, skipping insert.")
                await new_channel.delete()  # Clean up the channel if duplicate
                return None

            print("No duplicate found, adding to database...")
            db_webhook = Webhook(webhook_id=webhook.id, webhook_url=webhook.url, type=server)
            try:
                session.add(db_webhook)
                session.commit()
                recently_created_webhook_ids.add(str(webhook.id))
            except sqlalchemy.exc.IntegrityError as e:
                session.rollback()  # <--- CRITICAL: reset session after error
                print(f"IntegrityError: Webhook URL {webhook_url} already exists in database (race condition or autoflush). Skipping insert.")
                await new_channel.delete()  # Clean up the channel if duplicate
                return None

        print("Added to database, returning webhook data...")
        await asyncio.sleep(1)
        return {"webhook_id": webhook.id, "webhook_url": webhook_url}
    except Exception as e:
        session.rollback()  # Always rollback on any error
        print(f"Error creating new webhook: {e}")
        #await notification_channel.send(f"Couldn't create a new webhook:{e}")
        return 

## pending_deletion_cleanup_loop was removed on 2026-09-15. It deleted the whole
## channel of every webhook_pending_deletion row older than 96h, and
## check_missing_webhooks kept feeding it NULL-url rows (see
## tracked_webhook_ids), so one pool channel and its 5-15 live webhooks went
## every 4 days. Nothing here deletes a channel it didn't just create.

async def main():
    """Main function with systemd watchdog integration"""
    global watchdog

    # Setup signal handlers
    setup_signal_handlers()

    # Initialize systemd watchdog
    watchdog = SystemdWatchdog()
    watchdog.set_health_check(health_check)
    exit_for_restart = False

    try:
        async with watchdog:
            # Notify systemd that we're ready
            await watchdog.notify_ready()
            print("Systemd watchdog initialized and ready notification sent")

            started_at = time.monotonic()
            bot_task = asyncio.create_task(run_bot_client())
            stuck_task = asyncio.create_task(wait_until_disconnected_too_long())
            shutdown_task = asyncio.create_task(shutdown_event.wait())

            # Wait for a shutdown signal, the client stopping, or a gateway that won't come back.
            done, pending = await asyncio.wait(
                [bot_task, stuck_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            if shutdown_event.is_set():
                print("Shutdown requested, stopping bot...")
            else:
                exit_for_restart = True
                if bot_task in done:
                    error = None if bot_task.cancelled() else bot_task.exception()
                    reason = f"client stopped ({error!r})" if error else "client stopped"
                else:
                    reason = f"gateway down for {NOT_READY_EXIT_SECONDS}s"
                print(f"Heartbeat bot {reason}; exiting so systemd starts a fresh process.")
                remaining = MIN_RUN_SECONDS - (time.monotonic() - started_at)
                if remaining > 0:
                    try:
                        await asyncio.wait_for(shutdown_event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass

            for task in (bot_task, stuck_task, shutdown_task):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(shutdown_bot_client(), timeout=15)
            except asyncio.TimeoutError:
                print("Timed out closing the heartbeat bot client.")

            print("Heartbeat bot shutting down gracefully...")

    except KeyboardInterrupt:
        print("Received keyboard interrupt")
    except Exception as e:
        print(f"Fatal error in main: {e}")
        raise
    finally:
        print("Heartbeat bot cleanup completed")

    if exit_for_restart and not shutdown_event.is_set():
        # Exit non-zero so Restart=always brings up a fresh client, skipping
        # asyncio.run's teardown (a detached interactions shard task can hang it).
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

if __name__ == "__main__":
    print("Starting bot...")
    asyncio.run(main())




