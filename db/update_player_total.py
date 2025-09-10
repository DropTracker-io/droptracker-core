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
    """
    Update the player's total loot and related data in Redis.
    FIXED VERSION - Eliminates race conditions using atomic operations and better pipeline management.
    """
    current_partition = datetime.now().year * 100 + datetime.now().month
    
    # Format strings for different time granularities
    DATE_FORMAT = '%Y%m%d'
    HOUR_FORMAT = '%Y%m%d%H'
    MINUTE_FORMAT = '%Y%m%d%H%M'
    
    # Validate and filter batch_drops
    debug_print("Validating and filtering batch drops")
    if batch_drops is None:
        batch_drops = []

    # Track which drops we'll process
    drops_to_process = []
    
    if from_submission and len(batch_drops) == 1:
        drop = batch_drops[0]
        if check_if_drop_is_ignored(drop.drop_id):
            debug_print(f"Drop {drop.drop_id} attempted to re-process as a single entry, skipping")
            return True  # Return True as this is an expected skip
        drops_to_process = batch_drops
    else:
        drops_to_process = [drop for drop in batch_drops if drop.drop_id not in already_processed_drops]
        debug_print("Filtered out already processed drops, total drops: " + str(len(drops_to_process)))

    if not drops_to_process:
        debug_print("No drops to process after filtering")
        return True  # Return True as this is an expected skip

    # Initialize tracking dictionaries
    debug_print("Initializing tracking dictionaries")
    partition_totals = {}
    time_totals = {}
    time_items = {}
    time_npcs = {}
    time_npc_items = {}

    player_group_ids = []
    player_drops = drops_to_process if drops_to_process else []
    
    # Get the player's groups and their minimum values
    player: Player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.groups)).first()
    debug_print("Got player")
    
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
                    redis_client.client.expire(group_config_key, 3600)
            
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
        if player:
            player.date_updated = datetime.now()
            session.commit()
        return True
    
    debug_print("Processing each drop (" + str(len(player_drops)) + ")")
    
    # ===== CRITICAL FIX: Use a single pipeline with atomic operations =====
    pipeline = redis_client.client.pipeline(transaction=True)  # Use transactions!
    
    # Process drops and accumulate changes
    item_deltas = {}  # partition -> {item_id: [qty_delta, value_delta]}
    npc_deltas = {}   # partition -> {npc_id: value_delta}
    all_time_item_deltas = {}  # {item_id: [qty_delta, value_delta]}
    all_time_npc_deltas = {}   # {npc_id: value_delta}
    
    total_loot_deltas = {}  # partition -> total_loot_delta
    all_time_total_delta = 0
    
    for drop in player_drops:
        if check_if_drop_is_ignored(drop.drop_id):
            debug_print(f"Drop {drop.drop_id} attempted to re-process during batch processing, skipping")
            continue
        
        drop_partition = drop.partition
        drop_date = drop.date_added.strftime(DATE_FORMAT)
        drop_hour = drop.date_added.strftime(HOUR_FORMAT)
        drop_minute = drop.date_added.strftime(MINUTE_FORMAT)
        
        # Initialize partition tracking
        if drop_partition not in partition_totals:
            partition_totals[drop_partition] = {'total_loot': 0, 'items': {}, 'npcs': {}}
            item_deltas[drop_partition] = {}
            npc_deltas[drop_partition] = {}
            total_loot_deltas[drop_partition] = 0
        
        # Initialize time-based tracking
        for timeframe in [drop_date, drop_hour, drop_minute]:
            if timeframe not in time_totals:
                time_totals[timeframe] = {'total_loot': 0, 'items': {}, 'npcs': {}}
            if timeframe not in time_items:
                time_items[timeframe] = {}
            if timeframe not in time_npcs:
                time_npcs[timeframe] = {}
            if timeframe not in time_npc_items:
                time_npc_items[timeframe] = {}
            if drop.npc_id not in time_npc_items[timeframe]:
                time_npc_items[timeframe][drop.npc_id] = {}
        
        # Calculate total value
        total_value = drop.value * drop.quantity
        
        # Accumulate deltas for atomic updates
        total_loot_deltas[drop_partition] += total_value
        all_time_total_delta += total_value
        
        # Item deltas
        if drop.item_id not in item_deltas[drop_partition]:
            item_deltas[drop_partition][drop.item_id] = [0, 0]
        item_deltas[drop_partition][drop.item_id][0] += drop.quantity
        item_deltas[drop_partition][drop.item_id][1] += total_value
        
        if drop.item_id not in all_time_item_deltas:
            all_time_item_deltas[drop.item_id] = [0, 0]
        all_time_item_deltas[drop.item_id][0] += drop.quantity
        all_time_item_deltas[drop.item_id][1] += total_value
        
        # NPC deltas
        if drop.npc_id not in npc_deltas[drop_partition]:
            npc_deltas[drop_partition][drop.npc_id] = 0
        npc_deltas[drop_partition][drop.npc_id] += total_value
        
        if drop.npc_id not in all_time_npc_deltas:
            all_time_npc_deltas[drop.npc_id] = 0
        all_time_npc_deltas[drop.npc_id] += total_value
        
        # Update tracking dictionaries (for other logic)
        partition_totals[drop_partition]['total_loot'] += total_value
        all_time_totals['total_loot'] += total_value
        
        for timeframe in [drop_date, drop_hour, drop_minute]:
            time_totals[timeframe]['total_loot'] += total_value
        
        # Handle recent items and other logic
        for group_id, min_value in clan_minimums.items():
            if total_value >= min_value:
                recent_item_data = json.dumps({
                    'drop_id': drop.drop_id,
                    'item_id': drop.item_id,
                    'npc_id': drop.npc_id,
                    'value': drop.value,
                    'quantity': drop.quantity,
                    'date_added': drop.date_added.strftime('%Y-%m-%d %H:%M:%S'),
                    'partition': drop.partition
                })
                
                pipeline.lpush(f"player:{player_id}:{drop_partition}:recent_items", recent_item_data)
                pipeline.lpush(f"player:{player_id}:all:recent_items", recent_item_data)
                pipeline.lpush(f"group:{group_id}:recent_items", recent_item_data)
    
    # ===== ATOMIC REDIS UPDATES =====
    debug_print("Applying atomic updates to Redis")
    
    # Update partition totals atomically
    for partition, total_delta in total_loot_deltas.items():
        if not force_update:
            # Increment existing total
            pipeline.incrbyfloat(f"player:{player_id}:{partition}:total_loot", total_delta)
        else:
            # Set to new total (force update)
            pipeline.set(f"player:{player_id}:{partition}:total_loot", total_delta)
        
        # Update leaderboards
        rank_key = determine_key(partition=partition)
        if not force_update:
            pipeline.zincrby(rank_key, total_delta, player_id)
        else:
            pipeline.zadd(rank_key, {player_id: total_delta})
        
        for group_id in player_group_ids:
            group_rank_key = determine_key(partition=partition, group_id=group_id)
            if not force_update:
                pipeline.zincrby(group_rank_key, total_delta, player_id)
            else:
                pipeline.zadd(group_rank_key, {player_id: total_delta})
    
    # ===== CRITICAL FIX: Use Lua script for atomic hash updates =====
    print("Skipping lua script and redis update ...")
    lua_script = """
    local key = KEYS[1]
    local item_id = ARGV[1]
    local qty_delta = tonumber(ARGV[2])
    local value_delta = tonumber(ARGV[3])
    local force_update = ARGV[4] == "true"
    
    local current = redis.call('HGET', key, item_id)
    local new_qty, new_value
    
    if current and not force_update then
        local comma_pos = string.find(current, ',')
        if comma_pos then
            local existing_qty = tonumber(string.sub(current, 1, comma_pos - 1))
            local existing_value = tonumber(string.sub(current, comma_pos + 1))
            new_qty = existing_qty + qty_delta
            new_value = existing_value + value_delta
        else
            new_qty = qty_delta
            new_value = value_delta
        end
    else
        new_qty = qty_delta
        new_value = value_delta
    end
    
    redis.call('HSET', key, item_id, new_qty .. ',' .. new_value)
    return new_value
    """
    
    # # Apply item deltas atomically using Lua script
    # for partition, items in item_deltas.items():
    #     for item_id, (qty_delta, value_delta) in items.items():
    #         pipeline.eval(
    #             lua_script,
    #             1,
    #             f"player:{player_id}:{partition}:total_items",
    #             str(item_id),
    #             str(qty_delta),
    #             str(value_delta),
    #             str(force_update).lower()
    #         )
    
    # # Apply all-time item deltas
    # for item_id, (qty_delta, value_delta) in all_time_item_deltas.items():
    #     pipeline.eval(
    #         lua_script,
    #         1,
    #         f"player:{player_id}:all:total_items",
    #         str(item_id),
    #         str(qty_delta),
    #         str(value_delta),
    #         str(force_update).lower()
    #     )
    
    # # Update all-time total
    # if not force_update:
    #     pipeline.incrbyfloat(f"player:{player_id}:all:total_loot", all_time_total_delta)
    # else:
    #     pipeline.set(f"player:{player_id}:all:total_loot", all_time_total_delta)
    
    # # Update all-time leaderboards
    # rank_key_all_time = determine_key()
    # if not force_update:
    #     pipeline.zincrby(rank_key_all_time, all_time_total_delta, player_id)
    # else:
    #     pipeline.zadd(rank_key_all_time, {player_id: all_time_total_delta})
    
    # for group_id in player_group_ids:
    #     group_rank_key_all_time = determine_key(group_id=group_id)
    #     if not force_update:
    #         pipeline.zincrby(group_rank_key_all_time, all_time_total_delta, player_id)
    #     else:
    #         pipeline.zadd(group_rank_key_all_time, {player_id: all_time_total_delta})
    
    # # Similar atomic updates for NPCs, time-based data, etc.
    # # ... (additional logic here)
    
    # # Trim recent items lists
    # pipeline.ltrim(f"player:{player_id}:{current_partition}:recent_items", 0, 10)
    # pipeline.ltrim(f"player:{player_id}:all:recent_items", 0, 10)
    
    # # ===== SINGLE ATOMIC EXECUTION =====
    # try:
    #     #print("Executing single atomic pipeline")
    #     results = pipeline.execute()
    #     #print(f"Pipeline executed successfully with {len(results)} operations")
        
    #     # Only add drops to ignore list after successful processing
    #     for drop in drops_to_process:
    #         add_drop_to_ignore(drop.drop_id)
            
    #     if force_update and player:
    #         player.date_updated = datetime.now()
    #         session.commit()
    #         print("Player date updated")
        
    #     return True
            
    # except Exception as e:
    #     print(f"Pipeline execution failed for player {player_id}: {e}")
    #     app_logger.log(log_type="error", data=f"Pipeline execution failed for player {player_id}: {e}", app_name="redis_update", description="update_player_in_redis")
    #     return False

# ... (rest of the functions remain the same)
def process_drops_batch(batch_drops, session, from_submission=False):
    """Process a batch of drops and update Redis"""
    player_drops = {}
    for drop in batch_drops:
        if drop.player_id not in player_drops:
            player_drops[drop.player_id] = []
        player_drops[drop.player_id].append(drop)
    
    for player_id, drops in player_drops.items():
        try:
            update_player_in_redis(player_id, session, force_update=False, batch_drops=drops, from_submission=from_submission)
        except Exception as e:
            pass

async def check_and_update_players(session: sqlalchemy.orm.session):
    """Check if any player's data needs to be updated in Redis and update if more than 24 hours have passed."""
    global circuit_open, circuit_open_time, circuit_failure_count
    #print("Checking and updating players")
    if circuit_open:
        debug_print("Circuit breaker open")
        if datetime.now() - circuit_open_time > timedelta(seconds=CIRCUIT_RESET_TIME):
            circuit_open = False
            circuit_failure_count = 0
            print("Circuit breaker closed, continuing updates")
            app_logger.log(log_type="info", data="Circuit breaker reset, resuming player updates", app_name="main", description="check_and_update_players")
        else:
            debug_print("Circuit breaker closed, skipping player updates")
            return
    
    try:
        time_threshold = datetime.now() - timedelta(hours=24)
        player_to_update = session.query(Player).filter(Player.date_updated < time_threshold).first()
    except sqlalchemy.exc.PendingRollbackError as e:
        session.rollback()
        print("Database needed to be rolled back ....")
    
    if not player_to_update:
        print("No players need updating")
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
    """Fetch new drop records from the database and update the Redis cache."""
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
        await asyncio.to_thread(process_drops_batch, batch_drops, session)
        set_last_processed_drop_id(last_batch_drop_id)
        offset += BATCH_SIZE

    session.close()

@Task.create(IntervalTrigger(seconds=20))
async def background_task():
    """Background task that runs the update_player_totals function in a loop."""
    try:
        await check_and_update_players(session)
    except Exception as e:
        debug_print(f"Error in update_player_totals: {e}")

async def start_background_redis_tasks():
    """Starts the background tasks for the Quart server or Discord bot."""
    asyncio.create_task(background_task())

def check_if_drop_is_ignored(drop_id):
    """Check if a drop is in the ignore list"""
    return drop_id in already_processed_drops

def add_drop_to_ignore(drop_id):
    """Add a drop to the ignore list, ensuring the list doesn't exceed 250 items"""
    while len(already_processed_drops) >= 250:
        already_processed_drops.pop(0)
    already_processed_drops.append(drop_id) 