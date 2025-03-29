import asyncio
from cachetools import TTLCache
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker, joinedload
from db.models import Drop, Player, GroupConfiguration, session
from datetime import datetime, timedelta
import json
import os
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
    
    if force_update:
        # Wipe all player-related data from Redis
        keys_to_delete = redis_client.client.keys(f"player:{player_id}:*")
        if keys_to_delete:
            redis_client.client.delete(*keys_to_delete)
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

    player_drops = batch_drops if batch_drops else []

    # Initialize Redis pipeline
    pipeline = redis_client.client.pipeline(transaction=False)

    # Get the player's groups and their minimum values
    player = session.query(Player).filter(Player.player_id == player_id).options(joinedload(Player.groups)).first()
    clan_minimums = {}
    ## If player exists, we'll use their groups to store their recent submissions for the loot leaderboard.
    if player:
        for group in player.groups:
            ## Grab info on the players' groups so we can store their "recent submissions"-related items for the loot leaderboard.
            group_id = group.group_id
            ## check if we stored the group config in redis already to prevent excessive queries
            group_config_key = f"group_config:{group_id}"
            group_config = redis_client.client.hgetall(group_config_key)
            if group_config:
                ## Parse the config from redis
                group_config = parse_redis_data(group_config)
            if not group_config:
                ## otherwise let's query the DB anyways
                configs = session.query(GroupConfiguration).filter_by(group_id=group_id).all()
                group_config = {config.config_key: config.config_value for config in configs}
                if group_config:
                    redis_client.client.hset(group_config_key, mapping=group_config)
                    redis_client.client.expire(group_config_key, 3600)  # Cache for 1 hour
            
            clan_minimums[group_id] = int(group_config.get('minimum_value_to_notify', 2500000))

    ## Initialize the partition totals (a partition refers to a year/month combo of data) and all-time totals
    partition_totals = {}
    all_time_totals = {
        'total_loot': 0,
        'items': {},
        'npcs': {}
    }
    daily_totals = {}
    daily_items = {}    # Initialize empty dictionary
    daily_npcs = {}     # Initialize empty dictionary
## All dicts are initialized as empty here.
    for drop in player_drops:
        if check_if_drop_is_ignored(drop.drop_id):
            print(f"Drop {drop.drop_id} attempted to re-process during batch processing, skipping")
            continue
        drop_partition = drop.partition
        drop_date = drop.date_added.strftime('%Y%m%d')
    ## Here, we're formatting the {drop_date} variable as YYYYMMDD
        ## Get the date and the partition for the drop based on the database entry.
        # Initialize the dictionaries for this date if they don't exist
        if drop_date not in daily_totals:
            ## If the drop's date is not stored in daily_totals, we'll add it now.
            daily_totals[drop_date] = {
                'total_loot': 0,
                'items': {},
                'npcs': {}
            }
            ## This would create a new dict embedded in the daily_totals dict
            ## With a key of the drop_date and a value of a dict with keys 'total_loot', 'items', and 'npcs'
            ## Each of which has a value of 0 or an empty dict to begin with.
        ## Here, we're checking if the drop_date is not stored in daily_items or daily_npcs
        if drop_date not in daily_items:    # Add this check
            daily_items[drop_date] = {}
        ## If the drop_date is not stored in daily_items, we'll add it now.
        if drop_date not in daily_npcs:     # Add this check
            daily_npcs[drop_date] = {}
        ## If the drop_date is not stored in daily_npcs, we'll add it now.

        ## Sort through the list of drops and update totals in redis
        drop_partition = drop.partition  # Use the drop's actual partition (i.e, 202501 for jan 2025.)
        day_of_drop = drop.date_added.strftime('%Y%m%d')
    ## Again, we're getting YYYYMMDD for the {day_of_drop} variable.
        if day_of_drop not in daily_totals:
            ## If the drop_date is not stored in daily_totals, we'll add it now.
            daily_totals[day_of_drop] = {
                'total_loot': 0,
                'items': {},
                'npcs': {}
            }
        ## Here, we're checking if the drop_partition is not stored in partition_totals
        if drop_partition not in partition_totals:
            ## If this is the first time we've seen this partition, add it with empty values to the dict
            partition_totals[drop_partition] = {
                'total_loot': 0,
                'items': {},
                'npcs': {}
            }
        ## determine the total value of the drop based on quantity * value
        total_value = drop.value * drop.quantity
        
        # Update partition totals
        partition_totals[drop_partition]['total_loot'] += total_value
        if drop.item_id not in partition_totals[drop_partition]['items']:
            ## Add the item to the items dict with an empty set of values if it doesn't exist
            partition_totals[drop_partition]['items'][drop.item_id] = [0, 0]  # [qty, value]
        ## Add the quantity and value to the item's dict
        partition_totals[drop_partition]['items'][drop.item_id][0] += drop.quantity
        partition_totals[drop_partition]['items'][drop.item_id][1] += total_value
        
        # Update NPC totals for partition
        if drop.npc_id not in partition_totals[drop_partition]['npcs']:
            ## Add the npc to the npcs dict with an empty set of values if it doesn't exist
            partition_totals[drop_partition]['npcs'][drop.npc_id] = 0
        ## Add the total value to the npc's dict
        partition_totals[drop_partition]['npcs'][drop.npc_id] += total_value
        
        # Update all-time totals
        all_time_totals['total_loot'] += total_value
        if drop.item_id not in all_time_totals['items']:
            ## Add the item to the items dict with an empty set of values if it doesn't exist
            all_time_totals['items'][drop.item_id] = [0, 0]
        ## Add the quantity and value to the item's dict
        all_time_totals['items'][drop.item_id][0] += drop.quantity
        all_time_totals['items'][drop.item_id][1] += total_value
        if drop.npc_id not in all_time_totals['npcs']:
            ## Add the npc to the npcs dict with an empty set of values if it doesn't exist
            all_time_totals['npcs'][drop.npc_id] = 0
        ## Add the total value to the npc's dict
        all_time_totals['npcs'][drop.npc_id] += total_value

        # Handle recent items (only for current partition)
        if drop_partition == current_partition:
            for group_id, min_value in clan_minimums.items():
                if total_value >= min_value:
                    recent_item_data = {
                        "item_id": drop.item_id,
                        "npc_id": drop.npc_id,
                        "player_id": player_id,
                        "value": total_value,
                        "date_added": drop.date_added.isoformat()
                    }
                    recent_item_json = json.dumps(recent_item_data)
                    pipeline.lpush(f"player:{player_id}:{drop_partition}:recent_items", recent_item_json)
                    pipeline.lpush(f"player:{player_id}:all:recent_items", recent_item_json)
                    break

        # Add daily tracking alongside existing logic
        drop_date = drop.date_added.strftime('%Y%m%d')
        
        daily_totals[drop_date]['total_loot'] += total_value
        
        if drop.item_id not in daily_items[drop_date]:
            daily_items[drop_date][drop.item_id] = [0, 0]
        daily_items[drop_date][drop.item_id][0] += drop.quantity
        daily_items[drop_date][drop.item_id][1] += total_value
        
        if drop.npc_id not in daily_npcs[drop_date]:
            daily_npcs[drop_date][drop.npc_id] = 0
        daily_npcs[drop_date][drop.npc_id] += total_value

    # Store totals for each partition
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

    # Store all-time totals gathered from the loop after multiplying quantity * value for each drop in the matching partition
    pipeline.set(f"player:{player_id}:all:total_loot", all_time_totals['total_loot'])
    for item_id, (qty, value) in all_time_totals['items'].items():
        ## Store the total amount and quantity of each item the player has received (lootboard purposes)
        pipeline.hset(
            f"player:{player_id}:all:total_items",
            str(item_id),
            f"{qty},{value}"
        )
    for npc_id, value in all_time_totals['npcs'].items():
        ## Store the total amount of GP the player has received from each NPC (stored with the NPC ID as the key)
        pipeline.hset(
            f"player:{player_id}:all:npc_totals",
            str(npc_id),
            value
        )

    # Trim recent items lists to remove excess items if their group has a value set to 1 or some b.s.
    pipeline.ltrim(f"player:{player_id}:{current_partition}:recent_items", 0, 99)
    pipeline.ltrim(f"player:{player_id}:all:recent_items", 0, 99)

    # Add daily data storage to the existing pipeline
    for date, total in daily_totals.items():
        ## Here we check the daily_totals dict for the date and total stored for each date.
        ## We then store the total_loot for that date in redis.
        pipeline.set(f"player:{player_id}:daily:{date}:total_loot", total['total_loot'])
        
        if date in daily_items:  # Add safety check
            for item_id, (qty, value) in daily_items[date].items():
                pipeline.hset(
                    f"player:{player_id}:daily:{date}:items",
                    str(item_id),
                    f"{qty},{value}"
                )
        ## Here, we're storing the daily items for the player in redis.
        if date in daily_npcs:  # Add safety check
            for npc_id, value in daily_npcs[date].items():
                pipeline.hset(
                    f"player:{player_id}:daily:{date}:npcs",
                    str(npc_id),
                    value
                )
        ## Here, we're storing the daily npcs for the player in redis.
    # Execute all Redis commands
    pipeline.execute()

    if force_update:
        ## If the force_update flag is set, the player's cache was rendered invalid and wiped.
        ## We will update their player object to reflect this change
        player = session.query(Player).filter(Player.player_id == player_id).first()
        if player:
            player.date_updated = datetime.now()
            session.commit()

    # After updating player totals, update leaderboards
    # for partition in partition_totals.keys():
    #     leaderboard_manager.update_player_leaderboard_score(player_id, partition)
    
    # # Update all-time leaderboard
    # leaderboard_manager.update_player_leaderboard_score(player_id, 'all')

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
    if len(players_to_update) > 5:
        players_to_update = players_to_update[:1]
    for player in players_to_update:
        player_drops = session.query(Drop).filter(Drop.player_id == player.player_id).all()
        print(f"Updating player {player.player_id} in Redis (more than 24 hours since last update).")
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
    
    