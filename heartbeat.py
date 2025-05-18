import asyncio
from datetime import datetime, timedelta
import os
import random
import time
import aiohttp
import requests
from dotenv import load_dotenv
import interactions
from interactions import GuildText, IntervalTrigger, Permissions, Task, listen, slash_command
from interactions.api.events import Startup, MessageCreate
import json
import logging
from typing import Dict, List
from db.models import BackupWebhook, Webhook, NewWebhook, session, WebhookPendingDeletion
import sqlalchemy
from dotenv import load_dotenv
load_dotenv()
# Set up more detailed logging
# logging.basicConfig(level=logging.DEBUG)
import os
bot_token = os.getenv("HEARTBEAT_BOT_TOKEN")
bot = interactions.Client(token=bot_token)

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

url = "https://www.droptracker.io/api/heartbeat"

joel_id = 528746710042804247

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
                if wh_data['url'] == webhook.webhook_url:
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
    for guild in bot.guilds:
        for channel in guild.channels:
            try:
                if channel.type == interactions.ChannelType.GUILD_CATEGORY:
                    if channel.channels and channel.id in all_parent_ids:
                        for channel in channel.channels:
                            if isinstance(channel, GuildText):
                                # Initialize a new dictionary for this channel if it doesn't exist
                                if str(channel.id) not in webhook_states:
                                    webhook_states[str(channel.id)] = {}
                                
                                webhooks = await channel.fetch_webhooks()
                                if len(webhooks) > 0:
                                    for webhook in webhooks:
                                        existing_webhook = session.query(Webhook).filter_by(webhook_url=webhook.url).first()
                                        existing_pending_deletion = session.query(WebhookPendingDeletion).filter_by(webhook_url=webhook.url).first()
                                        # Only delete if not None
                                        if not existing_webhook and not existing_pending_deletion:
                                            try:
                                                new_webhook = Webhook(
                                                    webhook_id=webhook.id,
                                                    webhook_url=webhook.url,
                                                    type="core"
                                                )
                                                session.add(new_webhook)
                                                session.commit()
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
            except Exception as e:
                print(f"Error loading webhooks for channel {channel.id}: {str(e)}")
            finally:
                print(f"Finished loading from #{total_loaded}")
                total_loaded += 1
    print(f"Loaded webhook states for {len(webhook_states)} channels")
    await bot.change_presence(status=interactions.Status.ONLINE,
                              activity=interactions.Activity(name=f" {len(webhook_states)}({int(len(webhook_states) / 3)}) webhooks", type=interactions.ActivityType.WATCHING))
    

async def test_webhook(webhook, session):
    """Test a single webhook and return its status"""
    try:
        if len(str(webhook.webhook_url)) < 5:
            return {
                'webhook_id': webhook.webhook_id if hasattr(webhook, 'webhook_id') else 'pending_deletion',
                'url': webhook.webhook_url,
                'status': 'Error',
                'elapsed': 0,
                'ok': False
            }
        start_time = time.time()
        async with session.get(webhook.webhook_url, timeout=10) as response:
            elapsed = time.time() - start_time
            status = response.status
            return {
                'webhook_id': webhook.webhook_id if hasattr(webhook, 'webhook_id') else 'pending_deletion',
                'url': webhook.webhook_url,
                'status': status,
                'elapsed': elapsed,
                'ok': 200 <= status < 400
            }
    except aiohttp.ClientError as e:
        return {
            'webhook_id': webhook.webhook_id if hasattr(webhook, 'webhook_id') else 'pending_deletion',
            'url': webhook.webhook_url,
            'status': 'Error',
            'error': str(e),
            'ok': False
        }


@Task.create(IntervalTrigger(minutes=5))
async def test_all_webhooks():
    """Test all webhooks with a delay between requests"""
    webhooks = session.query(Webhook).all()
    secondary = session.query(WebhookPendingDeletion).all()
    all_webhooks = secondary + webhooks
    passed = 0
    failed = 0
    
    notification_channel = await bot.fetch_channel(1369649855194202223)
    await notification_channel.send(f"Performing a routine route check on {len(all_webhooks)} webhooks...")
    results = []
    async with aiohttp.ClientSession() as http_session:
        for i, webhook in enumerate(all_webhooks):
            print(f"Testing webhook {i+1}/{len(all_webhooks)}: {webhook.webhook_url}...")
            result = await test_webhook(webhook, http_session)
            results.append(result)
            
            # Print result immediately
            if result['ok']:
                print(f"✅ {result['url']} - Status: {result['status']} ({result['elapsed']:.2f}s)")
                passed += 1
            else:
                await notification_channel.send(f"❌ {result['status']} - FAILED: {result['url']}")
                failed += 1
                ## Remove it from the database
                session.delete(webhook)
                session.commit()
                print("Skipping creation for now...")
                #await create_new_webhook(should_create=True)
            # Add delay between requests (2 seconds)
            if i < len(all_webhooks) - 1:  # Don't delay after the last request
                await asyncio.sleep(1)
    await notification_channel.send(f"Completed a routine route check on {len(all_webhooks)} webhooks. {passed}/{len(all_webhooks)} passed, {failed}/{len(all_webhooks)} failed.")
    return results

@Task.create(IntervalTrigger(minutes=10))
async def check_missing_webhooks():
    existing_webhooks = session.query(Webhook).all()
    existing_pending_deletions = session.query(WebhookPendingDeletion).all()
    all_webhooks = existing_webhooks + existing_pending_deletions
    total_added = 0
    for guild in bot.guilds:
        for channel in guild.channels:
            if channel.type == interactions.ChannelType.GUILD_CATEGORY:
                if channel.id in all_parent_ids:
                    if channel.channels:
                        for channel in channel.channels:
                            if isinstance(channel, GuildText):
                                webhooks = await channel.fetch_webhooks()
                                if len(webhooks) > 0:
                                    for webhook in webhooks:
                                        existing_webhook = session.query(Webhook).filter_by(webhook_url=webhook.url).first()
                                        existing_pending_deletion = session.query(WebhookPendingDeletion).filter_by(webhook_url=webhook.url).first()
                                        if existing_webhook is None and existing_pending_deletion is None:
                                            print(f"Missing webhook needs to be added to the database: {webhook.url}")
                                            new_webhook = WebhookPendingDeletion(
                                                webhook_id=webhook.id,
                                                webhook_url=webhook.url,
                                                channel_id=channel.id,
                                                date_added=datetime.now(),
                                                date_updated=datetime.now()
                                            )
                                            session.add(new_webhook)
                                            total_added += 1
    # for channel_id, webhooks_dict in webhook_states.items():
    #     for webhook_id, webhook_data in webhooks_dict.items():
    #         existing_webhook = session.query(Webhook).filter_by(webhook_url=webhook_data['url']).first()
    #         if existing_webhook is None:
    #             existing_pending_deletion = session.query(WebhookPendingDeletion).filter_by(webhook_url=webhook_data['url']).first()
    #             if existing_pending_deletion is None:
    #                 print(f"Missing webhook needs to be added to the database: {webhook_data['url']}")
    #                 # Delete the webhook from the webhook_states
    #                 new_webhook = WebhookPendingDeletion(
    #                     webhook_id=webhook_id,
    #                     webhook_url=webhook_data['url'],
    #                     channel_id=channel_id,
    #                     date_added=datetime.now(),
    #                     date_updated=datetime.now()
    #                 )
    #                 session.add(new_webhook)
    #                 total_added += 1
    #         else:
    #             print(f"Webhook with ID {existing_webhook.webhook_id} already exists in the database, skipping...")
    # session.commit()
    session.commit()
    print(f"Added {total_added} missing webhooks to the database")

@Task.create(IntervalTrigger(seconds=10))
async def heartbeat_loop():
    response = requests.get(url, params={"key": os.getenv("HEARTBEAT_TOKEN")})
    if response.status_code == 200:
        data = response.json()
        if data["closed_status"] == True or data["ready_status"] == False or not data["message_id"]:
            notification_channel = await bot.fetch_channel(1369649855194202223)
            await notification_channel.send("@everyone\n" + 
                                            "Bot appears to have died....... I am restarting it...")
            await run_restart()
        else:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            #print(f"[{timestamp}] Heartbeat check passed.")
    elif response.status_code == 403:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] Invalid heartbeat token... exiting...")
        exit()
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            print(response.json())
        except Exception as e:
            print(f"[{timestamp}] Could not decode JSON from heartbeat response: {e}")
            print(f"Response text: {response.text}")

        
async def run_restart():
    script = "./restart.sh"
    try:
        await asyncio.create_subprocess_shell(script)
    except Exception as e:
        print(f"Error running restart script: {e}")


@Task.create(IntervalTrigger(minutes=30))
async def run_channel_deletes():#
    ## Actually acts as a webhook replacement loop instread of channel deletions
    global main_parent_ids, hooks_parent_ids, hooks_2_parent_ids, hooks_3_parent_ids
    parent_ids = main_parent_ids + hooks_parent_ids + hooks_2_parent_ids + hooks_3_parent_ids
    notification_channel = await bot.fetch_channel(1369649855194202223)
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
                    
                                # Determine a good webhook name based on channel name
                                webhook_name = f"DropTracker Webhooks ({channel.name.replace('drops-', '')})"
                                
                                webhook: interactions.Webhook = await channel.create_webhook(name=webhook_name, avatar=avatar)
                                webhook_url = webhook.url
                                print("Created webhook, checking for duplicates in database...")
                                with session.no_autoflush:
                                    existing = session.query(Webhook).filter_by(webhook_url=webhook_url).first()
                                    if existing:
                                        print(f"Webhook URL {webhook_url} already exists in database, skipping insert.")
                                        return
                                    print("No duplicate found, adding to database...")
                                    # Determine webhook type based on channel's parent category
                                    webhook_type = "backup"
                                    if channel.parent_id in main_parent_ids:
                                        webhook_type = "core"
                                    elif channel.parent_id in hooks_parent_ids:
                                        webhook_type = "hooks"
                                    elif channel.parent_id in hooks_2_parent_ids:
                                        webhook_type = "hooks-2"
                                    elif channel.parent_id in hooks_3_parent_ids:
                                        webhook_type = "hooks-3"
                                        
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
                                
                except Exception as e:
                    print(f"Error deleting channel {channel.name}: {e}")


@listen(Startup)
async def on_startup():
    print("Bot starting up -- loading webhook states...")
    await check_missing_webhooks()
    # await test_all_webhooks()
    # await run_new_webhook_loop()
    await load_initial_webhook_states()
    # #print("Checking for missing webhooks in the database based on the current webhook states...")
    # check_missing_webhooks.start()
    # await check_missing_webhooks()
    # test_all_webhooks.start()
    # await test_all_webhooks()
    heartbeat_loop.start()
    await run_channel_deletes()
    run_channel_deletes.start()
    # print("Returned from channel delete func")
    #run_new_webhook_loop.start()
    #await run_new_webhook_loop()
    # await asyncio.sleep(10)
    # pending_deletion_cleanup_loop.start()

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
    
    # Handle new webhooks
    if new_webhooks:
        print(f"New webhooks created in channel {channel.name}:")
        #notification_channel = await bot.fetch_channel(1369649855194202223)
        #await notification_channel.send(f"{len(new_webhooks)} new webhook creations were detected.")
        return
    
    # Handle deleted webhooks
    if deleted_webhooks:
        print(f"Webhooks deleted from channel {channel.name}:")
        notification_channel = await bot.fetch_channel(1369649855194202223)
        for webhook_id, webhook_data in deleted_webhooks.items():
            print(f"  - Name: {webhook_data['name']}, ID: {webhook_id}")
            pending_changes.add(channel.id)
            # Delete from database
            try:
                result = session.query(Webhook).filter(Webhook.webhook_url == webhook_data['url']).delete()
                if not result:
                    result = session.query(BackupWebhook).filter(BackupWebhook.webhook_url == webhook_data['url']).delete()
                    if not result:
                        # Also check WebhookPendingDeletion table
                        result = session.query(WebhookPendingDeletion).filter(WebhookPendingDeletion.webhook_url == webhook_data['url']).delete()
            except Exception as e:
                print(f"Error deleting webhook from database: {e}")
            
            session.commit()
            
            # Send notification after all operations
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
        
                    # Determine a good webhook name based on channel name
                    webhook_name = f"DropTracker Webhooks ({channel.name.replace('drops-', '')})"
                    
                    webhook: interactions.Webhook = await channel.create_webhook(name=webhook_name, avatar=avatar)
                    webhook_url = webhook.url
                    print("Created webhook, checking for duplicates in database...")
                    with session.no_autoflush:
                        existing = session.query(Webhook).filter_by(webhook_url=webhook_url).first()
                        if existing:
                            print(f"Webhook URL {webhook_url} already exists in database, skipping insert.")
                            return
                        print("No duplicate found, adding to database...")
                        # Determine webhook type based on channel's parent category
                        webhook_type = "backup"
                        if channel.parent_id in main_parent_ids:
                            webhook_type = "core"
                        elif channel.parent_id in hooks_parent_ids:
                            webhook_type = "hooks"
                        elif channel.parent_id in hooks_2_parent_ids:
                            webhook_type = "hooks-2"
                        elif channel.parent_id in hooks_3_parent_ids:
                            webhook_type = "hooks-3"
                            
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

@Task.create(IntervalTrigger(hours=1))
async def pending_deletion_cleanup_loop():
    """
    Periodically deletes channels/webhooks that have been pending deletion for over 96 hours.
    """
    now = datetime.now()
    threshold = now - timedelta(hours=96)
    pending = session.query(WebhookPendingDeletion).filter(WebhookPendingDeletion.date_added < threshold).all()
    for entry in pending:
        try:
            # Fetch the channel from the webhook URL (extract channel ID if stored, or store it in the table)
            webhook_url = entry.webhook_url
            # You may need to store channel_id in WebhookPendingDeletion for easier lookup!
            # If not, fetch the webhook and get its channel_id
            channel_id = entry.channel_id
            if channel_id:
                channel = await bot.fetch_channel(channel_id)
                await channel.delete()
            else:
                print(f"Could not determine channel for webhook {entry.webhook_id}")
        except Exception as e:
            print(f"Error deleting channel/webhook for {entry.webhook_id}: {e}")
        session.delete(entry)
    session.commit()  # Run every hour

if __name__ == "__main__":
    print("Starting bot...")
    bot.start()




