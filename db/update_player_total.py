import asyncio
import aiohttp
from cachetools import TTLCache
from interactions import IntervalTrigger, Task
from sqlalchemy import create_engine, func
import sqlalchemy
from sqlalchemy.orm import sessionmaker, joinedload
from db.models import Drop, Player, GroupConfiguration, session
from datetime import datetime, timedelta
import json
import os
from utils.keys import determine_key
from utils.wiseoldman import get_player_metric, get_player_metric_sync
from utils.redis import RedisClient
from utils.format import parse_redis_data
import logging
from db.app_logger import AppLogger

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
app_logger = AppLogger()
already_processed_drops = []


debug_level = os.getenv("DEBUG_LEVEL", "info")
debug = debug_level != "false"

circuit_open = False
circuit_open_time = None
circuit_failure_count = 0
CIRCUIT_THRESHOLD = 5  # Number of failures before opening circuit
CIRCUIT_RESET_TIME = 300

def debug_print(message):
    global debug
    if debug:
        print(message)

def get_last_processed_drop_id():
    return int(redis_client.get(LAST_DROP_ID_KEY) or 0)

def set_last_processed_drop_id(drop_id):
    redis_client.set(LAST_DROP_ID_KEY, drop_id)

def update_player_in_redis(player_id, session, force_update=False, batch_drops=None, from_submission=False):
    """Update the player's total loot and related data in Redis."""
    current_partition = datetime.now().year * 100 + datetime.now().month
    
    # Format strings for different time granularities
    DATE_FORMAT = '%Y%m%d'
    HOUR_FORMAT = '%Y%m%d%H'
    MINUTE_FORMAT = '%Y%m%d%H%M'
    
    # Validate and filter batch_drops
    debug_print("Validating and filtering batch drops")
    if batch_drops is None: # Ensure batch_drops is a list
        batch_drops = []

    if from_submission and len(batch_drops) == 1:
        drop = batch_drops[0]
        if check_if_drop_is_ignored(drop.drop_id):
            debug_print(f"Drop {drop.drop_id} attempted to re-process as a single entry, skipping")
            return
    else:
        # Filter out already processed drops for batches or non-submission single drops]
        debug_print("Not coming from a submission, filtering out already processed drops")
        batch_drops = [drop for drop in batch_drops if drop.drop_id not in already_processed_drops]
        debug_print("Filtered out already processed drops, total drops: " + str(len(batch_drops)))
    # Removed the problematic override of force_update based on len(batch_drops)
    # The force_update parameter passed to the function will now be respected.

    # Initialize tracking dictionaries
    debug_print("Initializing tracking dictionaries")
    partition_totals = {}
    partition_items = {}
    partition_npcs = {}
    
    # Initialize time-based tracking dictionaries
    time_totals = {}  # Will store totals for all time granularities
    time_items = {}   # Will store items for all time granularities
    time_npcs = {}    # Will store NPCs for all time granularities
    time_npc_items = {}  # Will store items by NPC for all time granularities

    player_group_ids = []
    
    player_drops = batch_drops if batch_drops else []
    
    # Initialize Redis pipeline
    pipeline = redis_client.client.pipeline(transaction=False)
    
    # Get the player's groups and their minimum values
    player: Player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.groups)).first()
    debug_print("Got player")
    # try:
    #     player_log_slots = get_player_metric_sync(player.player_name, "collections_logged")
    #     player.log_slots = player_log_slots
    #     session.commit()
    # except Exception as e:
    #     debug_print(f"Error updating player log slots for {player.player_name}: {e}")
    #     session.rollback()
    clan_minimums = {}
    
    if player:
        for group in player.groups:
            group_id = group.group_id
            group_config_key = f"group_config:{group_id}"
            group_config = redis_client.client.hgetall(group_config_key)
            player_group_ids.append(group_id)
            if group_config:
                group_config = parse_redis_data(group_config)
            
            if not group_config:
                configs = session.query(GroupConfiguration).filter_by(group_id=group_id).all()
                group_config = {config.config_key: config.config_value for config in configs}
                if group_config:
                    redis_client.client.hset(group_config_key, mapping=group_config)
                    redis_client.client.expire(group_config_key, 3600)  # Cache for 1 hour
            
            clan_minimums[group_id] = int(group_config.get('minimum_value_to_notify', 2500000))
    debug_print("Got player groups and minimum values")
    # Initialize partition and all-time totals
    partition_totals = {}
    all_time_totals = {
        'total_loot': 0,
        'items': {},
        'npcs': {}
    }
    
    # Process each drop
    if len(player_drops) == 0:
        debug_print("No drops to process")
        player.date_updated = datetime.now()
        session.commit()
        return True
    debug_print("Processing each drop (" + str(len(player_drops)) + ")")
    pipeline_count = 0
    for drop in player_drops:
        pipeline_count += 1
        if pipeline_count >= 1000:
            pipeline.execute()
            pipeline_count = 0
        if check_if_drop_is_ignored(drop.drop_id):
            debug_print(f"Drop {drop.drop_id} attempted to re-process during batch processing, skipping")
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
                debug_print("Stored recent item data in this partition: " + recent_item_data)
                
                # Add to all-time recent items
                pipeline.lpush(f"player:{player_id}:all:recent_items", recent_item_data)
                
                # Add to group recent items
                pipeline.lpush(f"group:{group_id}:recent_items", recent_item_data)
        
        # Mark this drop as processed
        add_drop_to_ignore(drop.drop_id)
    pipeline.execute()
    debug_print("Executed and processed all drops, storing totals in Redis")
    # Store partition totals in Redis
    pipeline_count = 0
    for partition, totals in partition_totals.items():
        pipeline_count += 1
        if pipeline_count >= 500:
            pipeline.execute()
            pipeline_count = 0
        
        currentTotal = 0
        if not force_update:
            debug_print("Not forcing update")
            currentTotalBytes = redis_client.client.zscore(f"leaderboard:{partition}", player_id)
            if currentTotalBytes is not None:  # Changed from 'if currentTotalBytes:'
                try:
                    # Handle both float and bytes cases
                    if isinstance(currentTotalBytes, float):
                        currentTotal = int(currentTotalBytes)  # Direct conversion if already a float
                    else:
                        currentTotal = int(float(currentTotalBytes.decode('utf-8')))  # Decode if bytes
                except (ValueError, AttributeError):
                    app_logger.log(log_type="warning", data=f"Malformed zscore for player {player_id}, partition {partition}", app_name="redis_update", description="update_player_in_redis")
                    currentTotal = 0
            # else: currentTotal remains 0
        # else: currentTotal remains 0 for force_update=True

        partition_total = totals['total_loot'] + currentTotal
        pipeline.set(f"player:{player_id}:{partition}:total_loot", partition_total)
        rank_key = determine_key(partition=partition)
        ## Store the player's total in the zset for this partition
        pipeline.zadd(rank_key, {player_id: partition_total})
        debug_print("player is in " + str(player_group_ids))
        for group_id in player_group_ids:
            rank_key = determine_key(partition=partition, group_id=group_id)
            ## Store the player's total in the zset for this partition
            debug_print("Set value for " + str(rank_key) + " to " + str(partition_total))
            pipeline.zadd(rank_key, {player_id: partition_total})

        # Store item totals
        if not force_update:
            raw_existing_items = redis_client.client.hgetall(f"player:{player_id}:{partition}:total_items")
            existing_items_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in raw_existing_items.items()}
            debug_print("No force update, got existing items data")
        else:
            existing_items_data = {}
            debug_print("Force update, no existing items data")
        for item_id, (qty, value) in totals['items'].items():
            existing_qty, existing_value = 0, 0
            if not force_update:
                item_id_str = str(item_id)
                if item_id_str in existing_items_data:
                    try:
                        existing_qty, existing_value = map(int, existing_items_data[item_id_str].split(','))
                    except ValueError:
                        app_logger.log(log_type="warning", data=f"Malformed item data for player {player_id}, partition {partition}, item {item_id_str}", app_name="redis_update", description="update_player_in_redis")
                        existing_qty, existing_value = 0,0
            
            pipeline.hset(
                f"player:{player_id}:{partition}:total_items",
                str(item_id),
                f"{qty + existing_qty},{value + existing_value}"
            )
        # Store NPC totals
        if not force_update:
            raw_existing_npcs = redis_client.client.hgetall(f"player:{player_id}:{partition}:npc_totals")
            existing_npcs_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in raw_existing_npcs.items()}
        else:
            existing_npcs_data = {}

        for npc_id, value in totals['npcs'].items():
            existing_value = 0
            if not force_update:
                npc_id_str = str(npc_id)
                if npc_id_str in existing_npcs_data:
                    try:
                        existing_value = int(existing_npcs_data[npc_id_str])
                    except ValueError:
                        app_logger.log(log_type="warning", data=f"Malformed NPC data for player {player_id}, partition {partition}, NPC {npc_id_str}", app_name="redis_update", description="update_player_in_redis")
                        existing_value = 0
            
            total_npc_value = value + existing_value
            pipeline.hset(
                f"player:{player_id}:{partition}:npc_totals",
                str(npc_id),
                total_npc_value
            )
            ## Store the player's total in the zset for this npc/partition combination
            rank_key_npc = determine_key(npc_id=npc_id, partition=partition)
            pipeline.zadd(rank_key_npc, {player_id: total_npc_value})
            for group_id in player_group_ids:
                rank_key_group_npc = determine_key(npc_id=npc_id, partition=partition, group_id=group_id)
                pipeline.zadd(rank_key_group_npc, {player_id: total_npc_value})
    debug_print("Stored partition totals, working on all-time totals")
    pipeline.execute()
    ## Check the player's stored total_items for this partition ...
    raw_existing_items = redis_client.client.hgetall(f"player:{player_id}:{partition}:total_items")
    existing_items_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in raw_existing_items.items()}
    debug_print("EXISTING ITEMS DATA: " + str(existing_items_data))
    # Store all-time totals
    current_all_time_total_val = 0
    if not force_update:
        current_all_time_total_bytes = redis_client.client.get(f"player:{player_id}:all:total_loot")
        if current_all_time_total_bytes:
            try:
                current_all_time_total_val = int(current_all_time_total_bytes.decode('utf-8'))
            except ValueError:
                app_logger.log(log_type="warning", data=f"Malformed all-time total loot for player {player_id}", app_name="redis_update", description="update_player_in_redis")
                current_all_time_total_val = 0
    # else: current_all_time_total_val remains 0 (consistent with original force_update=True logic)
    
    all_time_total = all_time_totals['total_loot'] + current_all_time_total_val
    pipeline.set(f"player:{player_id}:all:total_loot", all_time_total)
    ## Set the player's total in the zset for all time
    rank_key_all_time = determine_key()
    pipeline.zadd(rank_key_all_time, {player_id: all_time_total})
    for group_id in player_group_ids:
        group_rank_key_all_time = determine_key(group_id=group_id)
        pipeline.zadd(group_rank_key_all_time, {player_id: all_time_total})

    for item_id, (qty, value) in all_time_totals['items'].items():
        existing_qty, existing_value = 0, 0
        if not force_update:
            existing_item_bytes = redis_client.client.hget(f"player:{player_id}:all:total_items", str(item_id))
            if existing_item_bytes:
                try:
                    existing_qty, existing_value = map(int, existing_item_bytes.decode('utf-8').split(','))
                except ValueError:
                    app_logger.log(log_type="warning", data=f"Malformed all-time item data for player {player_id}, item {item_id}", app_name="redis_update", description="update_player_in_redis")
                    existing_qty, existing_value = 0,0
        # else: existing_qty, existing_value remain 0,0 (consistent with original force_update=True logic)
        
        total_item_qty = qty + existing_qty
        total_item_value = value + existing_value
        pipeline.hset(
            f"player:{player_id}:all:total_items",
            str(item_id),
            f"{total_item_qty},{total_item_value}"
        )
        ## Store the player's total value for this item in the zset for this item/all time combination
        rank_key_item_all_time = determine_key(item_id=item_id)
        pipeline.zadd(rank_key_item_all_time, {player_id: total_item_value}) # Use item-specific value
        for group_id in player_group_ids: # Added group-specific leaderboard for all-time items
            group_rank_key_item_all_time = determine_key(item_id=item_id, group_id=group_id)
            pipeline.zadd(group_rank_key_item_all_time, {player_id: total_item_value})
    pipeline.execute()
    for npc_id, value in all_time_totals['npcs'].items():
        existing_value = 0
        if not force_update:
            existing_value_bytes = redis_client.client.hget(f"player:{player_id}:all:npc_totals", str(npc_id))
            if existing_value_bytes:
                try:
                    existing_value = int(existing_value_bytes.decode('utf-8'))
                except ValueError:
                    app_logger.log(log_type="warning", data=f"Malformed all-time NPC data for player {player_id}, NPC {npc_id}", app_name="redis_update", description="update_player_in_redis")
                    existing_value = 0
        # else: existing_value remains 0 (consistent with original force_update=True logic)
        
        total_npc_value_all_time = value + existing_value
        pipeline.hset(
            f"player:{player_id}:all:npc_totals",
            str(npc_id),
            total_npc_value_all_time
        )
        ## Store the player's total in the zset for this npc/all time combination
        rank_key_npc_all_time = determine_key(npc_id=npc_id)
        pipeline.zadd(rank_key_npc_all_time, {player_id: total_npc_value_all_time})
        for group_id in player_group_ids:
            rank_key_group_npc_all_time = determine_key(npc_id=npc_id, group_id=group_id)
            pipeline.zadd(rank_key_group_npc_all_time, {player_id: total_npc_value_all_time})
    debug_print("Stored all-time totals, trimming recent items lists")
    debug_print("Original list: " + str(redis_client.client.lrange(f"player:{player_id}:{current_partition}:recent_items", 0, -1)))

    # Trim recent items lists to 10 items
    pipeline.ltrim(f"player:{player_id}:{current_partition}:recent_items", 0, 10)
    pipeline.ltrim(f"player:{player_id}:all:recent_items", 0, 10)
    debug_print("Trimmed recent items lists, storing time-based data in Redis")
    debug_print("New list: " + str(redis_client.client.lrange(f"player:{player_id}:{current_partition}:recent_items", 0, -1)))
    # Store time-based data in Redis with appropriate prefixes
    pipeline_count = 0
    for timeframe, total_data in time_totals.items(): # Renamed 'total' to 'total_data' for clarity
        pipeline_count += 1
        if pipeline_count >= 1000:
            pipeline.execute()
            pipeline_count = 0
        
        prefix = ""
        ttl = 0
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
        current_time_frame_total_val = 0
        if not force_update:
            current_time_total_bytes = redis_client.client.get(f"player:{player_id}:{prefix}:{timeframe}:total_loot")
            if current_time_total_bytes:
                try:
                    current_time_frame_total_val = int(current_time_total_bytes.decode('utf-8'))
                except ValueError:
                    app_logger.log(log_type="warning", data=f"Malformed time-based total loot for player {player_id}, timeframe {timeframe}", app_name="redis_update", description="update_player_in_redis")
                    current_time_frame_total_val = 0
            currentTotalBytes = redis_client.client.zscore(f"leaderboard:{timeframe}", player_id)
            if currentTotalBytes is not None:  # Changed from 'if currentTotalBytes:'
                try:
                    # Handle both float and bytes cases
                    if isinstance(currentTotalBytes, float):
                        currentTotal = int(currentTotalBytes)  # Direct conversion if already a float
                    else:
                        currentTotal = int(float(currentTotalBytes.decode('utf-8')))  # Decode if bytes
                except (ValueError, AttributeError):
                    app_logger.log(log_type="warning", data=f"Malformed zscore for player {player_id}, partition {partition}", app_name="redis_update", description="update_player_in_redis")
                    currentTotal = 0
        # else: current_time_frame_total_val remains 0 (consistent with original force_update=True logic)

        new_total_loot_for_timeframe = total_data['total_loot'] + current_time_frame_total_val
        key_total_loot_tf = f"player:{player_id}:{prefix}:{timeframe}:total_loot"
        pipeline.set(key_total_loot_tf, new_total_loot_for_timeframe)
        if ttl > 0: pipeline.expire(key_total_loot_tf, ttl)
        
        # Store in the leaderboard zset (not using SET on the leaderboard key)
        rank_key_tf = determine_key(partition=timeframe)
        pipeline.zadd(rank_key_tf, {player_id: new_total_loot_for_timeframe})
        if ttl > 0: pipeline.expire(rank_key_tf, ttl)
        
        # Add to group-specific time-based leaderboards
        for group_id in player_group_ids:
            group_rank_key_tf = determine_key(partition=timeframe, group_id=group_id)
            pipeline.zadd(group_rank_key_tf, {player_id: new_total_loot_for_timeframe})
            if ttl > 0: pipeline.expire(group_rank_key_tf, ttl)
        
        

    # Execute all Redis commands
    pipeline.execute()
    
    if force_update:
        debug_print("Force update flag set")
        # If the force_update flag is set, the player's cache was rendered invalid and wiped.
        # We will update their player object to reflect this change
        player = session.query(Player).filter(Player.player_id == player_id).first()
        if player:
            player.date_updated = datetime.now()
            session.commit()
            return True
    
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

async def check_and_update_players(session: sqlalchemy.orm.session):
    """
    Check if any player's data needs to be updated in Redis and update if more than 24 hours have passed.
    """
    global circuit_open, circuit_open_time, circuit_failure_count
    
    # Check if circuit is open
    if circuit_open:
        if datetime.now() - circuit_open_time > timedelta(seconds=CIRCUIT_RESET_TIME):
            # Reset circuit after timeout
            circuit_open = False
            circuit_failure_count = 0
            app_logger.log(log_type="info", data="Circuit breaker reset, resuming player updates", app_name="main", description="check_and_update_players")
        else:
            debug_print("Circuit breaker open, skipping player updates")
            return
    
    try:
        time_threshold = datetime.now() - timedelta(hours=24)
        player_to_update = session.query(Player).filter(Player.date_updated < time_threshold).first()
    except sqlalchemy.exc.PendingRollbackError as e:
        session.rollback()
        debug_print("Database needed to be rolled back ....")
    
    original_length = 0
    if player_to_update:
        debug_print("Found a player to update")
    
    if not player_to_update:
        debug_print("No players need updating")
        return
    
    endpoint = "http://localhost:21475/update"

    async def update_player(player):
        global circuit_failure_count
        try:
            async with aiohttp.ClientSession() as session_http:
                async with session_http.post(
                    endpoint, 
                    json={"player_id": player.player_id},
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        debug_print(f"Updated player {player.player_id} in Redis.")
                        # Reset failure count on success
                        circuit_failure_count = 0
                    else:
                        debug_print(f"Failed to update player {player.player_id}: Status {response.status}")
                        app_logger.log(log_type="error", data=f"Failed to update player {player.player_id}: Status {response.status}", app_name="main", description="check_and_update_players")
                        circuit_failure_count += 1
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            debug_print(f"Connection error updating player {player.player_id}: {e}")
            app_logger.log(log_type="error", data=f"Connection error updating player {player.player_id}: {e}", app_name="main", description="check_and_update_players")
            circuit_failure_count += 1
        except Exception as e:
            debug_print(f"Error updating player {player.player_id}: {e}")
            app_logger.log(log_type="error", data=f"Error updating player {player.player_id}: {e}", app_name="main", description="check_and_update_players")
            circuit_failure_count += 1
        
        # Check if we should open the circuit
        if circuit_failure_count >= CIRCUIT_THRESHOLD:
            circuit_open = True
            circuit_open_time = datetime.now()
            app_logger.log(log_type="warning", data=f"Circuit breaker opened after {circuit_failure_count} failures", app_name="main", description="check_and_update_players")
    
    try:
        await update_player(player_to_update)
    except Exception as e:
        debug_print(f"Error in update_player: {e}")
        app_logger.log(log_type="error", data=f"Error in update_player: {e}", app_name="main", description="check_and_update_players")

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

@Task.create(IntervalTrigger(seconds=20))
async def background_task():
    """
    Background task that runs the update_player_totals function in a loop,
    ensuring the cache is updated periodically without stalling the main application.
    """
    debug_print("Background task cycle")
    try:
        await check_and_update_players(session)
    except Exception as e:
        debug_print(f"Error in update_player_totals: {e}")
        #app_logger.log(log_type="error", data=f"Error in update_player_totals: {e}", app_name="main", description="background_task")

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
    
    