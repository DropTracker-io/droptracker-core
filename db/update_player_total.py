import asyncio
from cachetools import TTLCache
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, joinedload
from db.models import Drop, Player, GroupConfiguration, session
from datetime import datetime, timedelta
import json
import os
from utils.wiseoldman import get_player_metric, get_player_metric_sync
from utils.redis import RedisClient
from utils.format import parse_redis_data
import logging


# Initialize Redis
redis_client = RedisClient()
handled_list = []
# Redis Keys
LAST_DROP_ID_KEY = "last_processed_drop_id"

# Batch size for pagination
BATCH_SIZE = 2500  # Number of drops processed at once


# At the top of the file, after the imports
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

already_processed_drops = []


def get_last_processed_drop_id():
    return int(redis_client.get(LAST_DROP_ID_KEY) or 0)

def set_last_processed_drop_id(drop_id):
    redis_client.set(LAST_DROP_ID_KEY, drop_id)

def update_player_in_redis(player_id, session, force_update=True, batch_drops=None, from_submission=False):
    """Update the player's total loot and related data in Redis."""
    current_partition = datetime.now().year * 100 + datetime.now().month
    
    # Format strings for different time granularities
    DATE_FORMAT = '%Y%m%d'
    HOUR_FORMAT = '%Y%m%d%H'
    MINUTE_FORMAT = '%Y%m%d%H%M'
    
    if force_update:
        # Wipe all player-related data from Redis
        keys_to_delete = redis_client.client.keys(f"player:{player_id}:*")
        if keys_to_delete:
            redis_client.client.delete(*keys_to_delete)
    
    # Validate and filter batch_drops
    if len(batch_drops) == 1 and from_submission == True:
        drop = batch_drops[0]
        if check_if_drop_is_ignored(drop.drop_id):
            print(f"Drop {drop.drop_id} attempted to re-process as a single entry, skipping")
            return
    else:
        old_drops = []
        batch_drops = [drop for drop in batch_drops if drop.drop_id not in already_processed_drops]
    
    # Initialize tracking dictionaries
    partition_totals = {}
    partition_items = {}
    partition_npcs = {}
    
    # Initialize time-based tracking dictionaries
    time_totals = {}  # Will store totals for all time granularities
    time_items = {}   # Will store items for all time granularities
    time_npcs = {}    # Will store NPCs for all time granularities
    time_npc_items = {}  # Will store items by NPC for all time granularities
    
    player_drops = batch_drops if batch_drops else []
    
    # Initialize Redis pipeline
    pipeline = redis_client.client.pipeline(transaction=False)
    
    # Get the player's groups and their minimum values
    player: Player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.groups)).first()
    # try:
    #     player_log_slots = get_player_metric_sync(player.player_name, "collections_logged")
    #     player.log_slots = player_log_slots
    #     session.commit()
    # except Exception as e:
    #     print(f"Error updating player log slots for {player.player_name}: {e}")
    #     session.rollback()
    clan_minimums = {}
    
    if player:
        for group in player.groups:
            group_id = group.group_id
            group_config_key = f"group_config:{group_id}"
            group_config = redis_client.client.hgetall(group_config_key)
            
            if group_config:
                group_config = parse_redis_data(group_config)
            
            if not group_config:
                configs = session.query(GroupConfiguration).filter_by(group_id=group_id).all()
                group_config = {config.config_key: config.config_value for config in configs}
                if group_config:
                    redis_client.client.hset(group_config_key, mapping=group_config)
                    redis_client.client.expire(group_config_key, 3600)  # Cache for 1 hour
            
            clan_minimums[group_id] = int(group_config.get('minimum_value_to_notify', 2500000))
    
    # Initialize partition and all-time totals
    partition_totals = {}
    all_time_totals = {
        'total_loot': 0,
        'items': {},
        'npcs': {}
    }
    
    # Process each drop
    for drop in player_drops:
        if check_if_drop_is_ignored(drop.drop_id):
            print(f"Drop {drop.drop_id} attempted to re-process during batch processing, skipping")
            continue
        
        drop_partition = drop.partition
        
        # Get timestamps at different granularities
        drop_date = drop.date_added.strftime(DATE_FORMAT)
        drop_hour = drop.date_added.strftime(HOUR_FORMAT)
        drop_minute = drop.date_added.strftime(MINUTE_FORMAT)
        
        # Initialize partition dictionaries if needed
        if drop_partition not in partition_totals:
            partition_totals[drop_partition] = {
                'total_loot': 0,
                'items': {},
                'npcs': {}
            }
        
        # Initialize time-based dictionaries for all granularities
        for timeframe in [drop_date, drop_hour, drop_minute]:
            if timeframe not in time_totals:
                time_totals[timeframe] = {
                    'total_loot': 0,
                    'items': {},
                    'npcs': {}
                }
            
            if timeframe not in time_items:
                time_items[timeframe] = {}
            
            if timeframe not in time_npcs:
                time_npcs[timeframe] = {}
        
        # Initialize NPC items tracking for all granularities
        for timeframe in [drop_date, drop_hour, drop_minute]:
            if timeframe not in time_npc_items:
                time_npc_items[timeframe] = {}
            
            if drop.npc_id not in time_npc_items[timeframe]:
                time_npc_items[timeframe][drop.npc_id] = {}
        
        # Calculate total value
        total_value = drop.value * drop.quantity
        
        # Update partition totals
        partition_totals[drop_partition]['total_loot'] += total_value
        
        # Update all-time totals
        all_time_totals['total_loot'] += total_value
        
        # Update time-based totals for all granularities
        for timeframe in [drop_date, drop_hour, drop_minute]:
            time_totals[timeframe]['total_loot'] += total_value
        
        # Update partition item totals
        if drop.item_id not in partition_totals[drop_partition]['items']:
            partition_totals[drop_partition]['items'][drop.item_id] = [0, 0]  # [qty, value]
        partition_totals[drop_partition]['items'][drop.item_id][0] += drop.quantity
        partition_totals[drop_partition]['items'][drop.item_id][1] += total_value
        
        # Update all-time item totals
        if drop.item_id not in all_time_totals['items']:
            all_time_totals['items'][drop.item_id] = [0, 0]  # [qty, value]
        all_time_totals['items'][drop.item_id][0] += drop.quantity
        all_time_totals['items'][drop.item_id][1] += total_value
        
        # Update time-based item totals for all granularities
        for timeframe in [drop_date, drop_hour, drop_minute]:
            if drop.item_id not in time_items[timeframe]:
                time_items[timeframe][drop.item_id] = [0, 0]  # [qty, value]
            time_items[timeframe][drop.item_id][0] += drop.quantity
            time_items[timeframe][drop.item_id][1] += total_value
        
        # Update partition NPC totals
        if drop.npc_id not in partition_totals[drop_partition]['npcs']:
            partition_totals[drop_partition]['npcs'][drop.npc_id] = 0
        partition_totals[drop_partition]['npcs'][drop.npc_id] += total_value
        
        # Update all-time NPC totals
        if drop.npc_id not in all_time_totals['npcs']:
            all_time_totals['npcs'][drop.npc_id] = 0
        all_time_totals['npcs'][drop.npc_id] += total_value
        
        # Update time-based NPC totals for all granularities
        for timeframe in [drop_date, drop_hour, drop_minute]:
            if drop.npc_id not in time_npcs[timeframe]:
                time_npcs[timeframe][drop.npc_id] = 0
            time_npcs[timeframe][drop.npc_id] += total_value
        
        # Update NPC item totals for all granularities
        for timeframe in [drop_date, drop_hour, drop_minute]:
            if drop.item_id not in time_npc_items[timeframe][drop.npc_id]:
                time_npc_items[timeframe][drop.npc_id][drop.item_id] = [0, 0]  # [qty, value]
            
            time_npc_items[timeframe][drop.npc_id][drop.item_id][0] += drop.quantity
            time_npc_items[timeframe][drop.npc_id][drop.item_id][1] += total_value
        
        # Check if this drop exceeds the clan's minimum value for notifications
        for group_id, min_value in clan_minimums.items():
            if total_value >= min_value:
                # Add to the player's recent items list for this group
                recent_item_data = json.dumps({
                    'drop_id': drop.drop_id,
                    'item_id': drop.item_id,
                    'npc_id': drop.npc_id,
                    'value': drop.value,
                    'quantity': drop.quantity,
                    'date_added': drop.date_added.strftime('%Y-%m-%d %H:%M:%S'),
                    'partition': drop.partition
                })
                
                # Add to partition recent items
                pipeline.lpush(f"player:{player_id}:{drop_partition}:recent_items", recent_item_data)
                
                # Add to all-time recent items
                pipeline.lpush(f"player:{player_id}:all:recent_items", recent_item_data)
                
                # Add to group recent items
                pipeline.lpush(f"group:{group_id}:recent_items", recent_item_data)
        
        # Mark this drop as processed
        add_drop_to_ignore(drop.drop_id)
    
    # Store partition totals in Redis
    for partition, totals in partition_totals.items():
        # Set total loot for partition
        currentTotal = redis_client.client.get(f"player:{player_id}:{partition}:total_loot")
        if currentTotal:
            currentTotal = int(currentTotal)
        else:
            currentTotal = 0
        pipeline.set(f"player:{player_id}:{partition}:total_loot", totals['total_loot'] + currentTotal)
        
        # Store item totals
        existing_items = redis_client.client.hgetall(f"player:{player_id}:{partition}:total_items")
        for item_id, (qty, value) in totals['items'].items():
            existing_qty, existing_value = (0, 0)
            if str(item_id) in existing_items:
                existing_qty, existing_value = map(int, existing_items[str(item_id)].split(','))
            pipeline.hset(
                f"player:{player_id}:{partition}:total_items",
                str(item_id),
                f"{qty + existing_qty},{value + existing_value}"
            )
        
        # Store NPC totals
        existing_npcs = redis_client.client.hgetall(f"player:{player_id}:{partition}:npc_totals")
        for npc_id, value in totals['npcs'].items():
            existing_value = int(existing_npcs.get(str(npc_id), 0))
            pipeline.hset(
                f"player:{player_id}:{partition}:npc_totals",
                str(npc_id),
                value + existing_value
            )
    
    # Store all-time totals
    pipeline.set(f"player:{player_id}:all:total_loot", all_time_totals['total_loot'])
    for item_id, (qty, value) in all_time_totals['items'].items():
        pipeline.hset(
            f"player:{player_id}:all:total_items",
            str(item_id),
            f"{qty},{value}"
        )
    for npc_id, value in all_time_totals['npcs'].items():
        pipeline.hset(
            f"player:{player_id}:all:npc_totals",
            str(npc_id),
            value
        )
    
    # Trim recent items lists
    pipeline.ltrim(f"player:{player_id}:{current_partition}:recent_items", 0, 99)
    pipeline.ltrim(f"player:{player_id}:all:recent_items", 0, 99)
    
    # Store time-based data in Redis with appropriate prefixes
    for timeframe, total in time_totals.items():
        # Determine the granularity level and set appropriate prefix and TTL
        if len(timeframe) == 8:  # YYYYMMDD (daily)
            prefix = "daily"
            ttl = 2592000  # 30 days
        elif len(timeframe) == 10:  # YYYYMMDDHH (hourly)
            prefix = "hourly"
            ttl = 604800  # 7 days
        elif len(timeframe) == 12:  # YYYYMMDDHHMM (minute)
            prefix = "minute"
            ttl = 86400  # 1 day
        
        # Store total loot for this timeframe
        pipeline.set(f"player:{player_id}:{prefix}:{timeframe}:total_loot", total['total_loot'])
        
        # Store item totals for this timeframe
        if timeframe in time_items:
            for item_id, (qty, value) in time_items[timeframe].items():
                pipeline.hset(
                    f"player:{player_id}:{prefix}:{timeframe}:items",
                    str(item_id),
                    f"{qty},{value}"
                )
        
        # Store NPC totals for this timeframe
        if timeframe in time_npcs:
            for npc_id, value in time_npcs[timeframe].items():
                pipeline.hset(
                    f"player:{player_id}:{prefix}:{timeframe}:npcs",
                    str(npc_id),
                    value
                )
        
        # Store NPC item totals for this timeframe
        if timeframe in time_npc_items:
            for npc_id, items in time_npc_items[timeframe].items():
                for item_id, (qty, value) in items.items():
                    pipeline.hset(
                        f"player:{player_id}:{prefix}:{timeframe}:npc_items:{npc_id}",
                        str(item_id),
                        f"{qty},{value}"
                    )
    
    # Execute all Redis commands
    pipeline.execute()
    
    if force_update:
        # If the force_update flag is set, the player's cache was rendered invalid and wiped.
        # We will update their player object to reflect this change
        player = session.query(Player).filter(Player.player_id == player_id).first()
        if player:
            player.date_updated = datetime.now()
            session.commit()
    
    return

def process_drops_batch(batch_drops, session, from_submission=False):
    ## This function is called during the processing of drops as they come in from webhooks
    ## Then, the update_player_in_redis function is called to update the player's cache with the force_update flag set to false
    
    """Process a batch of drops and update Redis"""
    # Group drops by player to process efficiently
    player_drops = {}
    for drop in batch_drops:
        if drop.player_id not in player_drops:
            player_drops[drop.player_id] = []
        player_drops[drop.player_id].append(drop)
    
    # Process each player's drops
    for player_id, drops in player_drops.items():
        try:
            # Pass the specific drops to process
            update_player_in_redis(player_id, session, force_update=False, batch_drops=drops, from_submission=from_submission)
            # logger.info(f"Processed {len(drops)} drops for player {player_id}")
        except Exception as e:
            # logger.error(f"Failed to process drops for player {player_id}: {e}")
            pass

def check_and_update_players(session):
    """
    Check if any player's data needs to be updated in Redis and update if more than 24 hours have passed.
    """
    # Get time threshold (24 hours ago)
    time_threshold = datetime.now() - timedelta(hours=24)

    # Query for players who need their data updated (older than 24 hours)
    players_to_update = session.query(Player).filter(Player.date_updated < time_threshold).all()
    original_length = 0
    if len(players_to_update) > 5:
        original_length = len(players_to_update)
        players_to_update = players_to_update[:1]
    for player in players_to_update:
        player_drops = session.query(Drop).filter(Drop.player_id == player.player_id).all()
        if original_length > 50:
            if player.player_id % 10 == 0:
                print(f"[MASS] Updating player {player.player_id} in Redis.")
        else:
            print(f"Updating player {player.player_id} in Redis.")
        update_player_in_redis(player.player_id, session, force_update=True, batch_drops=player_drops)

async def update_player_totals():
    """
        Fetch new drop records from the database and update the Redis cache.
        Process new drops and update player totals without modifying their `date_updated`.
    """
    DB_USER = os.getenv('DB_USER')
    DB_PASS = os.getenv('DB_PASS')
    engine = create_engine(f'mysql+pymysql://{DB_USER}:{DB_PASS}@localhost:3306/data')
    Session = sessionmaker(bind=engine)
    session = Session()

    last_drop_id = get_last_processed_drop_id()
    drop_count = session.query(func.count(Drop.drop_id)).filter(Drop.drop_id > last_drop_id).scalar()

    offset = 0
    while offset < drop_count:
        batch_drops = session.query(Drop).filter(Drop.drop_id > last_drop_id)\
                                         .order_by(Drop.drop_id.asc())\
                                         .limit(BATCH_SIZE)\
                                         .offset(offset)\
                                         .all()
        batch_drops = [drop for drop in batch_drops if drop.drop_id not in already_processed_drops]

        if not batch_drops:
            break

        last_batch_drop_id = batch_drops[-1].drop_id

        # Run the blocking code in a thread using asyncio.to_thread
        await asyncio.to_thread(process_drops_batch, batch_drops, session)

        # Verify some data was stored
                
        set_last_processed_drop_id(last_batch_drop_id)
        offset += BATCH_SIZE

    session.close()

async def background_task():
    """
    Background task that runs the update_player_totals function in a loop,
    ensuring the cache is updated periodically without stalling the main application.
    """
    while True:
        try:
            await update_player_totals()
            check_and_update_players(session)
        except Exception as e:
            logger.exception(f"Error in update_player_totals: {e}")
        await asyncio.sleep(5)  # Wait for 5 seconds before the next run

async def start_background_redis_tasks():
    """
    Starts the background tasks for the Quart server or Discord bot.
    This ensures that update_player_totals runs without blocking the main event loop.
    """
    asyncio.create_task(background_task())

def check_if_drop_is_ignored(drop_id):
    """
    Check if a drop is in the ignore list
    """
    if drop_id in already_processed_drops:
        return True
    else:
        return False

def add_drop_to_ignore(drop_id):
    """
    Add a drop to the ignore list, ensuring the list doesn't exceed 1000 items,
    otherwise deletes the oldest 500 items from the list
    """
    while len(already_processed_drops) >= 250:
        already_processed_drops.pop(0)
    already_processed_drops.append(drop_id)
    
    