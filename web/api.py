# api.py

import ast
import json
import os
import random
from utils import logger
from quart import Blueprint, jsonify, request, render_template, session, make_response
from quart_jwt_extended import (
    JWTManager,
    jwt_required,
    create_access_token,
    get_jwt_identity,
    decode_token
)
from db.models import CollectionLogEntry, CombatAchievementEntry, Webhook, user_group_association, NotifiedSubmission, User, Group, Guild, Player, NpcList, ItemList, PersonalBestEntry, Drop, UserConfiguration, session as sesh, ItemList, GroupConfiguration
from concurrent.futures import ThreadPoolExecutor
from db.ops import DatabaseOperations, associate_player_ids
import asyncio
from datetime import datetime, timedelta
from data.submissions import drop_processor
import interactions
from lootboard.generator import get_drops_for_group
from interactions.api.gateway.state import ConnectionState
#from xf.xenforo import XenForoAPI
from functools import lru_cache, wraps
from time import time
from cachetools import TTLCache
from io import StringIO
import csv

log_token = os.getenv("LOGGER_TOKEN")
logger = logger.LoggerClient(log_token)

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import func, select, text

from utils.format import format_number, parse_authed_users, convert_from_ms, convert_to_ms, get_current_partition
from utils.redis import RedisClient, calculate_clan_overall_rank
from utils.wiseoldman import fetch_group_members
executor = ThreadPoolExecutor()

#xf_api = XenForoAPI()
# Create a Blueprint object
redis_client = RedisClient()

# Create a TTL cache that holds items for 60 seconds
stats_cache = TTLCache(maxsize=1, ttl=60)

def cached_stats(func):
    """Decorator to cache stats with a 60-second TTL"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        cache_key = 'stats'
        
        # Try to get cached stats first
        cached_result = stats_cache.get(cache_key)
        if cached_result is not None:
            return jsonify(cached_result)

        # If not in cache, generate new stats
        try:
            total_drops = sesh.query(Drop.drop_id).count()
            total_pbs = sesh.query(PersonalBestEntry.pb_id).count()
            total_cas = sesh.query(CombatAchievementEntry.ca_id).count()
            total_clogs = sesh.query(CollectionLogEntry.clog_id).count()
            total_overall = total_drops + total_pbs + total_cas + total_clogs
            total_users = sesh.query(Player.player_id).count()
            
            result = {
                "total_drops": total_overall,
                "total_users": total_users,
                "cached_at": int(time()),
                "cache_ttl": 60
            }
            
            # Store in cache
            stats_cache[cache_key] = result
            return jsonify(result)
            
        except Exception as e:
            print(f"Error fetching stats: {e}")  # Add logging
            # Return fallback values if database query fails
            fallback = {
                "total_drops": 0,
                "total_users": 0,
                "cached_at": int(time()),
                "cache_ttl": 60,
                "error": "Failed to fetch stats"
            }
            return jsonify(fallback), 500
            
    return wrapper

## list of npcs tracked individually for side panel responses on RuneLite
target_list = ["Abyssal Sire", 
            "Alchemical Hydra", 
            "Araxxor",
            "Callisto", # Includes Callisto & Artio
            "Barrows Chest", # renamed to Barrows
            "Bryophyta",
            "Vet'ion", # Includes Vet'ion & Calvar'ion
            "Cerberus",
            "Chambers of Xeric", # Includes Chambers of Xeric & Chambers of Xeric (CM)
            "Chaos Elemental",
            "Chaos Fanatic",
            "Commander Zilyana",
            "Corporeal Beast",
            "Crazy Archaeologist",
            "Dagannoth Kings", # Includes Dagannoth Prime, Dagannoth Rex, Dagannoth Supreme
            "Deranged Archaeologist",
            "Duke Sucellus",
            "General Graardor",
            "Giant Mole",
            "Grotesque Guardians",
            "Kalphite Queen",
            "King Black Dragon",
            "Kraken",
            "Kree'Arra",
            "K'ril Tsutsaroth",
            "Lunar Chests", # Renamed to Perilous Moons
            "Nex",
            "Phosani's Nightmare", # includes nightmare
            "Phantom Muspah",
            "Sarachnis",
            "Scorpia",
            "Scurrius",
            "Skotizo",
            "Sol Heredit", # Renamed to Fortis Colosseum
            "Venenatis", # Includes Venenatis & Spindel
            "The Gauntlet", # Includes Corrupted Gauntlet and Gauntlet
            "The Leviathan",
            "The Whisperer",
            "Theatre of Blood", # Includes Theatre of Blood & Theatre of Blood (HM)
            "Thermonuclear Smoke Devil",
            "Tombs of Amascut", # Includes Tombs of Amascut & Tombs of Amascut (Expert mode)
            "Vardorvis",
            "Vorkath",
            "Zalcano",
            "Zulrah"]


def add_cors_headers(response):
    """Add CORS headers to any response"""
    if not response:
        response = jsonify({})
        
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

def create_api(bot: interactions.Client):
    api_blueprint = Blueprint('api', __name__)

    @api_blueprint.after_request
    async def after_request(response):
        """Add CORS headers to all responses"""
        return add_cors_headers(response)

    @api_blueprint.route('/')
    async def get_data():
        # Run the blocking function in a thread pool to avoid blocking the event loop
        #loop = asyncio.get_event_loop()
        #loop.run_in_executor(executor, update_player_totals)
        return jsonify({"message": "API is online."}), 200
        # Immediately return a response
        #return jsonify({"message": "Started background task to update player totals."})

    @api_blueprint.route('/get_channels', methods=['GET'])
    async def get_channels():
        try:
            guild_id = request.args.get('guild_id')
            channel_type = request.args.get('type', 'text')  # Default to text channels if not specified
            
            if not guild_id:
                return jsonify({"error": "No guild ID provided"}), 400
            
            guild = await bot.fetch_guild(int(guild_id))
            if not guild:
                return jsonify({"error": "Guild not found"}), 404
            
            channels = []
            # Get all channels and filter by type
            all_channels = await guild.fetch_channels()
            for channel in all_channels:
                # For text channels
                if channel_type == 'text' and isinstance(channel, interactions.GuildText):
                    channels.append({
                        'id': str(channel.id),
                        'name': channel.name
                    })
                # For voice channels
                elif channel_type == 'voice' and isinstance(channel, interactions.GuildVoice):
                    channels.append({
                        'id': str(channel.id),
                        'name': channel.name
                    })
                
            return jsonify(channels), 200
        
        except Exception as e:
            print(f"Error in get_channels: {str(e)}")
            return jsonify({"error": "Internal server error"}), 500
        
    @api_blueprint.route('/heartbeat', methods=['GET'])
    async def heartbeat():
        key = request.args.get('key')
        if key == os.getenv("HEARTBEAT_TOKEN"):
            ## Attempt to check the bot's heartbeat
            closed_status = bot.is_closed
            ready_status = bot.is_ready
            message = None
            ## Attempt to send a message
            try:
                channel = await bot.fetch_channel(1267605788445245521)
                message = await channel.fetch_messages(limit=1)
                if message:
                    message = message[0]
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"Error finding message: {e}")
                message = None
            return jsonify({"message": "Heartbeat received", "closed_status": closed_status, "ready_status": ready_status,
                            "message_id": message.id if message else None}), 200
            
        else:
            return jsonify({"error": "Invalid key"}), 403
        
    @api_blueprint.route('/csv_export', methods=['GET'])
    async def csv_export():
        ## Auth keys should be provided in the query params
        print(request.args.__dict__)
        key = request.args.get('key')
        correct_key = False  # Initialize to False
        correct_keys = sesh.query(GroupConfiguration).filter(GroupConfiguration.config_key == "export_api_key").all()
        print("Found", len(correct_keys), "correct keys")
        for correct_key_obj in correct_keys:
            if str(correct_key_obj.config_value).strip() == str(key).strip():
                correct_key = True
                break
        if not correct_key:
            print("Key did not match any valid keys:", [entry.config_value for entry in correct_keys])
            print("Key provided:", key)
            return jsonify({"error": "Invalid key"}), 403
            
        ## Find out what parameters this user is searching for
        _player = request.args.get('player', None)
        _group = request.args.get('group', None)
        _npc = request.args.get('npc', None)
        _start_time = request.args.get('start_time', None)
        _end_time = request.args.get('end_time', None)
        _start_date = request.args.get('start_date', None)
        _end_date = request.args.get('end_date', None)
        
        # Initialize lists to store target objects and IDs
        player_objects = []
        player_ids = []
        group_objects = []
        group_ids = []
        
        if _group:
            group_values = [g.strip() for g in _group.split(',')]

            for group_value in group_values:
                try:
                    # Try to parse as integer (ID)
                    group_id = int(group_value)
                    group_obj = sesh.query(Group).filter(Group.group_id == group_id).first()
                    if group_obj:
                        ## Get all players in the group
                        query = text("SELECT player_id FROM user_group_association WHERE group_id = :group_id")
                        group_player_ids = sesh.execute(query, {"group_id": group_id}).fetchall()
                        group_player_ids = [player_id[0] for player_id in group_player_ids]
                        # Accumulate player IDs
                        player_ids.extend(group_player_ids)
                        group_objects.append(group_obj)
                        group_ids.append(group_id)
                except ValueError:
                    # Handle as string (name)
                    group_obj = sesh.query(Group).filter(Group.group_name == group_value).first()
                    if group_obj:
                        group_objects.append(group_obj)
                        group_ids.append(group_obj.group_id)
            
            # If no valid groups were found, return error
            if not group_objects and _group:
                return jsonify({"error": "No valid groups found"}), 404

        ## Process player parameter (can be single value or comma-separated list)
        if _player:
            player_values = [p.strip() for p in _player.split(',')]
            for player_value in player_values:
                try:
                    # Try to parse as integer (ID)
                    player_id = int(player_value)
                    player_obj = sesh.query(Player).filter(Player.player_id == player_id).first()
                    if player_obj:
                        player_objects.append(player_obj)
                        player_ids.append(player_id)
                except ValueError:
                    # Handle as string (name)
                    player_obj = sesh.query(Player).filter(Player.player_name == player_value).first()
                    if player_obj:
                        player_objects.append(player_obj)
                        player_ids.append(player_obj.player_id)
            
            # If no valid players were found, return error
            if not player_objects and _player:
                return jsonify({"error": "No valid players found"}), 404
        
        
        
        # Process NPC parameter (can be single value or comma-separated list)
        npc_ids = []
        npc_objects = []
        if _npc:
            npc_values = [n.strip() for n in _npc.split(',')]
            for npc_value in npc_values:
                try:
                    # Try to parse as integer (ID)
                    npc_id = int(npc_value)
                    npc_obj = sesh.query(NpcList).filter(NpcList.npc_id == npc_id).first()
                    if npc_obj:
                        npc_ids.append(npc_id)
                        npc_objects.append(npc_obj)
                except ValueError:
                    # Handle as string (name)
                    npc_obj = sesh.query(NpcList).filter(NpcList.npc_name == npc_value).first()
                    if npc_obj:
                        npc_ids.append(npc_obj.npc_id)
                        npc_objects.append(npc_obj)
        
        # If no NPCs were specified, get all NPCs from the target list
        if not npc_ids:
            npc_dict = get_npc_ids_from_target_list()
            for npc_name, ids in npc_dict.items():
                for npc_id in ids:
                    npc_obj = sesh.query(NpcList).filter(NpcList.npc_id == npc_id).first()
                    if npc_obj:
                        npc_ids.append(npc_id)
                        npc_objects.append(npc_obj)
        
        # Process date and time parameters and determine optimal data retrieval strategy
        if not _start_date and not _end_date:
            # No date range specified - use all-time data
            use_all_time_data = True
            minutes_to_process = []
            days_to_process = []
            hours_to_process = []
        else:
            use_all_time_data = False
            
            # Process start date/time
            if _start_date:
                try:
                    # If start time is provided, use it; otherwise, use 00:00
                    if _start_time:
                        start_datetime = datetime.strptime(f"{_start_date}{_start_time}", "%Y%m%d%H%M")
                    else:
                        start_datetime = datetime.strptime(f"{_start_date}0000", "%Y%m%d%H%M")
                except ValueError:
                    return jsonify({"error": "Invalid start date or time format. Use YYYYMMDD for date and HHMM for time."}), 400
            else:
                # Default to 30 days ago if no start date is provided
                start_datetime = datetime.now() - timedelta(days=30)
            
            # Process end date/time
            if _end_date:
                try:
                    # If end time is provided, use it; otherwise, use 23:59
                    if _end_time:
                        end_datetime = datetime.strptime(f"{_end_date}{_end_time}", "%Y%m%d%H%M")
                    else:
                        end_datetime = datetime.strptime(f"{_end_date}2359", "%Y%m%d%H%M")
                except ValueError:
                    return jsonify({"error": "Invalid end date or time format. Use YYYYMMDD for date and HHMM for time."}), 400
            else:
                # Default to now if no end date is provided
                end_datetime = datetime.now()
            
            # Ensure start_datetime is before end_datetime
            if start_datetime > end_datetime:
                return jsonify({"error": "Start date/time must be before end date/time"}), 400
            
            # Initialize lists for different time granularities
            minutes_to_process = []
            hours_to_process = []
            days_to_process = []
            
            # Current position in the time range
            current = start_datetime
            
            # Process the start date partial day
            start_day = start_datetime.replace(hour=0, minute=0)
            next_day = (start_day + timedelta(days=1))
            
            if start_datetime.hour > 0 or start_datetime.minute > 0:
                # Handle partial hours at the start
                start_hour = start_datetime.replace(minute=0)
                next_hour = (start_hour + timedelta(hours=1))
                
                if start_datetime.minute > 0:
                    # Add individual minutes for the partial first hour
                    minute_current = start_datetime
                    while minute_current < min(next_hour, end_datetime):
                        minutes_to_process.append(minute_current.strftime("%Y%m%d%H%M"))
                        minute_current += timedelta(minutes=1)
                
                # Add complete hours for the rest of the start day
                hour_current = next_hour
                while hour_current < min(next_day, end_datetime):
                    hours_to_process.append(hour_current.strftime("%Y%m%d%H"))
                    hour_current += timedelta(hours=1)
            
            # Process complete days in the middle
            day_current = max(start_day, next_day)
            end_day = end_datetime.replace(hour=0, minute=0)
            
            while day_current < end_day:
                days_to_process.append(day_current.strftime("%Y%m%d"))
                day_current += timedelta(days=1)
            
            # Process the end date partial day
            if end_datetime > end_day:
                # Handle complete hours on the end day
                hour_current = end_day
                end_hour = end_datetime.replace(minute=0)
                
                while hour_current < end_hour:
                    hours_to_process.append(hour_current.strftime("%Y%m%d%H"))
                    hour_current += timedelta(hours=1)
                
                # Handle remaining minutes in the last partial hour
                if end_datetime.minute > 0:
                    minute_current = end_hour
                    while minute_current <= end_datetime:
                        minutes_to_process.append(minute_current.strftime("%Y%m%d%H%M"))
                        minute_current += timedelta(minutes=1)
            
            # Calculate total time points to process
            total_time_points = len(minutes_to_process) + len(hours_to_process) + len(days_to_process)
            
            # Log the optimization results
            print(f"Date range optimization: {len(days_to_process)} days, {len(hours_to_process)} hours, {len(minutes_to_process)} minutes")
            print(f"Total time points: {total_time_points} vs. {int((end_datetime - start_datetime).total_seconds() / 60)} minutes in range")

        # If no players were specified but groups were, get all players in those groups
        if not player_objects and group_objects:
            for group in group_objects:
                for player in group.players:
                    if player not in player_objects:
                        player_objects.append(player)
                        player_ids.append(player.player_id)
        
        # If no players or groups were specified, return an error
        if not player_objects:
            return jsonify({"error": "No players specified and no players found in specified groups"}), 400
        
        # Prepare data for CSV export
        csv_data = []
        
        # Add header row
        header = ["Player", "NPC", "Timestamp", "Item ID", "Item Name", "Quantity", "Value"]
        csv_data.append(header)
        
        # Process each player
        for player in player_objects:
            player_id = player.player_id
            player_name = player.player_name
            
            if use_all_time_data:
                # Use all-time data for the player
                all_items_key = f"player:{player_id}:all:total_items"
                all_items_data = redis_client.client.hgetall(all_items_key)
                all_items_data = redis_client.decode_data(all_items_data)
                
                all_npcs_key = f"player:{player_id}:all:npc_totals"
                all_npcs_data = redis_client.client.hgetall(all_npcs_key)
                all_npcs_data = redis_client.decode_data(all_npcs_data)
                
                # Filter by NPCs if specified
                for npc_id in npc_ids:
                    if str(npc_id) in all_npcs_data:
                        npc_obj = next((n for n in npc_objects if n.npc_id == npc_id), None)
                        if not npc_obj:
                            continue
                        
                        npc_name = npc_obj.npc_name
                        npc_value = int(all_npcs_data[str(npc_id)])
                        
                        # For all-time data, we don't have specific timestamps, so we use "All Time"
                        timestamp = "All Time"
                        
                        # Add a summary row for this NPC
                        csv_data.append([
                            player_name,
                            npc_name,
                            timestamp,
                            "",  # No specific item
                            "All Items",
                            "",  # No specific quantity
                            npc_value
                        ])
                        
                        # Add individual item breakdowns
                        for item_id, item_data in all_items_data.items():
                            qty, value = map(int, item_data.split(','))
                            
                            # Get item name
                            item_obj = sesh.query(ItemList).filter(ItemList.item_id == item_id).first()
                            item_name = item_obj.item_name if item_obj else f"Unknown Item ({item_id})"
                            
                            # Add row to CSV data
                            csv_data.append([
                                player_name,
                                npc_name,
                                timestamp,
                                item_id,
                                item_name,
                                qty,
                                value
                            ])
            else:
                # Process data at each time granularity
                
                # Process minute data
                for minute_key in minutes_to_process:
                    process_timeframe_data(player_id, player_name, "minute", minute_key, npc_ids, npc_objects, csv_data)
                
                # Process hour data
                for hour_key in hours_to_process:
                    process_timeframe_data(player_id, player_name, "hourly", hour_key, npc_ids, npc_objects, csv_data)
                
                # Process day data
                for day_key in days_to_process:
                    process_timeframe_data(player_id, player_name, "daily", day_key, npc_ids, npc_objects, csv_data)

        # If no data was found, return an error
        if len(csv_data) <= 1:  # Only header row
            return jsonify({"error": "No data found for the specified parameters"}), 404
        
        # Convert to CSV string
        csv_output = StringIO()
        csv_writer = csv.writer(csv_output)
        csv_writer.writerows(csv_data)
        
        # Return CSV file
        response = await make_response(csv_output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=droptracker_export.csv"
        response.headers["Content-type"] = "text/csv"
        
        return response
    

    def process_timeframe_data(player_id, player_name, prefix, timeframe, npc_ids, npc_objects, csv_data):
        # Check if there's any loot data for this timeframe
        total_loot_key = f"player:{player_id}:{prefix}:{timeframe}:total_loot"
        total_loot = redis_client.client.get(total_loot_key)
        
        if total_loot:
            # Format timestamp for display based on granularity
            if prefix == "minute":
                # For minute data, show the exact minute
                timestamp = datetime.strptime(timeframe, "%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M")
            elif prefix == "hourly":
                # For hourly data, show the hour range
                hour_start = datetime.strptime(timeframe, "%Y%m%d%H")
                hour_end = hour_start.replace(minute=59)
                timestamp = f"{hour_start.strftime('%Y-%m-%d %H:00')} - {hour_end.strftime('%H:%M')}"
            elif prefix == "daily":
                # For daily data, indicate it's for the entire day
                timestamp = f"{datetime.strptime(timeframe, '%Y%m%d').strftime('%Y-%m-%d')}"
            
            # Get all items for this timeframe
            items_key = f"player:{player_id}:{prefix}:{timeframe}:total_items"
            items_data = redis_client.client.hgetall(items_key)
            if items_data:
                items_data = redis_client.decode_data(items_data)
                
                # Get NPC totals for this timeframe
                npc_totals_key = f"player:{player_id}:{prefix}:{timeframe}:npc_totals"
                npc_totals = redis_client.client.hgetall(npc_totals_key)
                if npc_totals:
                    npc_totals = redis_client.decode_data(npc_totals)
                    
                    # Process each NPC
                    for npc_id in npc_ids:
                        npc_obj = next((n for n in npc_objects if n.npc_id == npc_id), None)
                        if not npc_obj:
                            continue
                            
                        npc_name = npc_obj.npc_name
                        
                        # Get NPC total if it exists
                        if str(npc_id) in npc_totals:
                            npc_total = int(npc_totals[str(npc_id)])
                            
                            # Add NPC total row
                            csv_data.append([
                                player_name,
                                npc_name,
                                timestamp,
                                "",  # No specific item
                                "All Items",
                                "",  # No specific quantity
                                npc_total
                            ])
                            
                            # Add each item to the CSV data
                            for item_id, item_data in items_data.items():
                                qty, value = map(int, item_data.split(','))
                                
                                # Get item name
                                item_obj = sesh.query(ItemList).filter(ItemList.item_id == item_id).first()
                                item_name = item_obj.item_name if item_obj else f"Unknown Item ({item_id})"
                                
                                # Add row to CSV data
                                csv_data.append([
                                    player_name,
                                    npc_name,
                                    timestamp,
                                    item_id,
                                    item_name,
                                    qty,
                                    value
                                ])
            return csv_data

    @api_blueprint.route('/player/<int:player_id>', methods=['GET'])
    async def get_player_data(player_id):
        """
            Endpoint to get a player's data from Redis.
            :param player_id: Player ID to retrieve data for.
            :query param partition: Optional partition parameter (defaults to current partition if not provided).
        """
        # Get the partition from the query parameter or default to the current partition
        partition = request.args.get('partition', default=datetime.now().year * 100 + datetime.now().month, type=int)

        # Define Redis keys for this player using the partition
        total_items_key_partition = f"player:{player_id}:{partition}:total_items"
        npc_totals_key_partition = f"player:{player_id}:{partition}:npc_totals"
        total_loot_key_partition = f"player:{player_id}:{partition}:total_loot"
        recent_items_key_partition = f"player:{player_id}:{partition}:recent_items"

        # Fetch data from Redis
        total_items = redis_client.client.hgetall(total_items_key_partition)
        npc_totals = redis_client.client.hgetall(npc_totals_key_partition)
        total_loot = redis_client.get(total_loot_key_partition)
        recent_items = redis_client.client.lrange(recent_items_key_partition, 0, -1)  # Get all recent items

        # Extract the data from bytes format
        total_items = redis_client.decode_data(total_items)
        npc_totals = redis_client.decode_data(npc_totals)
        recent_items = [item.decode('utf-8') for item in recent_items]

        # Return the data in JSON format
        return jsonify({
            "total_items": total_items,
            "npc_totals": npc_totals,
            "total_loot": total_loot,
            "recent_items": recent_items
        })

    @api_blueprint.route('/latest_welcome_message', methods=['GET'])
    async def get_latest_welcome_message():
        welcomeString = ""
        channel_id = 1283717633048444968
        channel = await bot.fetch_channel(channel_id)
        message_id = 1359303803366674525
        message = await channel.fetch_message(message_id)
        welcomeString = message.content
        print("Welcome string:", welcomeString)
        welcomeString = welcomeString.replace("```", "")
        if welcomeString:
            return str(welcomeString)
        else:
            return "No welcome message found", 404

    @api_blueprint.route('/latest_news', methods=['GET'])
    async def get_latest_news():
        updateString = ""
        channel_id = 1283717633048444968
        channel = await bot.fetch_channel(channel_id)
        message_id = 1359286032067203175
        message = await channel.fetch_message(message_id)
        updateString = message.content
        updateString = updateString.replace("```", "")
        if updateString:
            return str(updateString)
        else:
            return "No news found", 404
    
    
    @api_blueprint.route('/create_webhook', methods=['POST'])
    async def create_webhook():
        data = await request.get_json()
        key = data.get('key')
        if key == os.getenv("ENCRYPTION_KEY"):
            servers = ["main", "alt"]
            server = random.choice(servers)
            if server == "main":
                parent_id = 1211062421591167016
            else:
                parent_id = 1107479658267684976
            try:
                parent_channel = await bot.fetch_channel(parent_id)
                num = 35
                channel_name = f"drops-{num}"
                while channel_name in [channel.name for channel in parent_channel.channels]:
                    num += 1
                    channel_name = f"drops-{num}"
                new_channel: interactions.GuildText = await parent_channel.create_text_channel(channel_name)
                logo_path = '/store/droptracker/disc/static/assets/img/droptracker-small.gif'
                avatar = interactions.File(logo_path)
                webhook: interactions.Webhook = await new_channel.create_webhook(name=f"DropTracker Webhooks ({num})", avatar=avatar)
                webhook_url = webhook.url
                db_webhook = Webhook(webhook_url=str(webhook_url))
                session.add(db_webhook)
                session.commit()
                return jsonify({"message": "Webhook created successfully", "webhook_url": webhook_url, "webhook_id": db_webhook.webhook_id, "server": server}), 200
            except Exception as e:
                return jsonify({"message": "Couldn't create a new webhook", "error": str(e)}), 500
            pass
        else:
            return jsonify({"message": "Invalid key"}), 403
    
    
    @lru_cache(maxsize=1)
    def get_npc_ids():
        return get_npc_ids_from_target_list()

    @api_blueprint.route('/item-id', methods=['GET'])
    async def get_item_info():
        item_name = request.args.get('name')
        noted_param = request.args.get('noted', 'false').lower()
        normalized_name = item_name.strip().lower() if item_name else None
        noted = True if noted_param == 'true' else False
        item = sesh.query(ItemList).filter(ItemList.item_name == normalized_name,
                                            ItemList.noted == noted).first()
        # Logic for another route
        if item:
            return jsonify({"item_name": normalized_name,
                            "item_id": item.item_id,
                            "noted": noted}), 200
        else:
            return jsonify({"error": f"Item '{normalized_name}' not found."}), 404
        

    @api_blueprint.route('/player_lookup/<string:player_name>', methods=['GET'])
    async def player_lookup(player_name):
        player = sesh.query(Player).filter(Player.player_name == player_name).first()
        
        if not player:
            return jsonify({"message": "No data found for this player."}), 404
        
        if player.user:
            privacy_enabled_setting = sesh.query(UserConfiguration).filter(UserConfiguration.user_id == player.user.user_id, 
                                                            UserConfiguration.config_key == 'hide_me_globally').first()
            if privacy_enabled_setting:
                privacy = privacy_enabled_setting.config_value
                if privacy == "true":
                    return jsonify({"message": f"{player_name} has privacy enabled."})
        player_id = player.player_id
        wom_member_list = None
        if player.groups:
            for group in player.groups:
                group_wom_id = group.wom_id
                if group_wom_id:
                    wom_member_list = await fetch_group_members(wom_group_id=int(group_wom_id))
        
        clan_player_ids = wom_member_list if wom_member_list else []
        npc_ids = get_npc_ids_from_target_list()  # Fetch NPC IDs
        player_data = {}

        # Fetch and calculate loot and rank for each NPC
        partition = datetime.now().year * 100 + datetime.now().month
        looted_npcs = []
        for npc_name, npc_id_list in npc_ids.items():
            total_loot_partition = 0  # Total for this partition (monthly)
            total_loot_all = 0  # Total for all-time

            # Aggregate loot for all NPC IDs associated with this name
            for npc_id in npc_id_list:
                #print(f"Processing {npc_name} (ID: {npc_id})")

                # Fetch total loot for this NPC from Redis
                npc_loot_partition = redis_client.client.hget(f"player:{player_id}:{partition}:npc_totals", npc_id)
                npc_loot_all = redis_client.client.hget(f"player:{player_id}:all:npc_totals", npc_id)

                npc_loot_partition = int(npc_loot_partition) if npc_loot_partition else 0
                npc_loot_all = int(npc_loot_all) if npc_loot_all else 0

                #print(f"Total loot for {npc_name} - Partition: {npc_loot_partition}, All-time: {npc_loot_all}")

                # Skip NPCs the player has never received any loot from
                if npc_loot_all == 0:
                    continue

                # Add the loot totals for this NPC ID to the aggregate for the NPC name
                total_loot_partition += npc_loot_partition
                total_loot_all += npc_loot_all

            # If no loot was found for this NPC name, continue
            if total_loot_all == 0:
                continue

            # Store the NPC ID for which loot was found
            looted_npcs.append(npc_id)

            # Calculate the player's global and clan rank for this NPC
            global_loot_rank = calculate_global_rank(npc_id, player_id)
            clan_loot_rank = calculate_clan_npc_rank(npc_id=npc_id, player_id=player_id, total_loot=total_loot_partition, clan_player_ids=clan_player_ids)

            # Log the ranks for debugging purposes
            if global_loot_rank and clan_loot_rank:
                print(f"Global rank for {npc_name}: {global_loot_rank}, Clan rank: {clan_loot_rank}")

            # Build the player data for this NPC
            player_data[npc_name] = {
                "loot": {
                    "all-time": str(format_number(total_loot_all)),
                    "month": str(format_number(total_loot_partition))
                },
                "rank": {
                    "global": str(global_loot_rank) if global_loot_rank else "N/A",
                    "clan": str(clan_loot_rank) if clan_loot_rank else "N/A"
                }
            }
        player_pbs = sesh.query(PersonalBestEntry).filter(PersonalBestEntry.player_id == player_id).all()
        for pb in player_pbs:
            if pb.npc_id not in looted_npcs:
                continue
            npc_name = sesh.query(NpcList.npc_name).filter(NpcList.npc_id == pb.npc_id).first()[0]
            pb_time = convert_from_ms(pb.personal_best)

            pb_ranks = await calculate_personal_best_rank(sesh, pb.npc_id, player_id, pb.personal_best, clan_player_ids)
            global_pb_rank = pb_ranks.get("rank_global")
            clan_pb_rank = pb_ranks.get("rank_clan")
            
            if npc_name in player_data:
                player_data[npc_name]["PB"] = {
                    "time": str(pb_time),
                    "rank_global": str(global_pb_rank),
                    "rank_clan": str(clan_pb_rank)
                }
            else:
                player_data[npc_name] = {
                    "PB": {
                        "time": str(pb_time),
                        "rank_global": str(global_pb_rank),
                        "rank_clan": str(clan_pb_rank)
                    }
                }
        if player_data != {}:
            response_data = {
                "bossData": player_data
            }
        else:
            response_data = {
                "message": f"{player_name} has no loot tracked."
            }
        
        # print(f"Lookup received for {player_name}, returning", response_data)
        return jsonify(response_data)


    
    def is_user_authorized(user_id, group: Group):
        # Check if the user is an admin or an authorized user for this group
        group_config = sesh.query(GroupConfiguration).filter(GroupConfiguration.group_id == group.group_id).all()
        # Transform group_config into a dictionary for easy access
        config = {conf.config_key: conf.config_value for conf in group_config}
        authed_user = False
        user_data: User = sesh.query(User).filter(User.user_id == user_id).first()
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

    return api_blueprint

async def calculate_personal_best_rank(session, npc_id, player_id, player_pb_time, clan_player_ids):
    """
    Calculate the personal best rank for a player in both global and clan-specific contexts.
    :param session: SQLAlchemy session object
    :param npc_id: ID of the NPC
    :param player_id: ID of the player
    :param player_pb_time: Player's personal best time in ms
    :param clan_player_ids: List of player IDs in the same clan
    :return: A dictionary containing the global and clan PB rankings
    """
    # Fetch global ranking for the NPC based on PB
    all_ranks = sesh.query(PersonalBestEntry).filter(
        PersonalBestEntry.npc_id == npc_id
    ).order_by(PersonalBestEntry.personal_best.asc()).all()
    player_ids = await associate_player_ids(clan_player_ids)
    # Fetch clan-specific ranking for the NPC based on PB
    print("Calculating clan PB based on", len(player_ids), "player ID entries")
    group_ranks = sesh.query(PersonalBestEntry).filter(
        PersonalBestEntry.player_id.in_(player_ids),
        PersonalBestEntry.npc_id == npc_id
    ).order_by(PersonalBestEntry.personal_best.asc()).all()

    # Calculate global and clan placement for the current player
    global_placement = None
    clan_placement = None

    # Global placement calculation
    for idx, entry in enumerate(all_ranks, start=1):
        if entry.player_id == player_id and entry.personal_best == player_pb_time:
            global_placement = idx
            break

    # Clan placement calculation
    for idx, entry in enumerate(group_ranks, start=1):
        if entry.player_id == player_id and entry.personal_best == player_pb_time:
            clan_placement = idx
            break

    return {
        "rank_global": global_placement,
        "rank_clan": clan_placement
    }

def calculate_global_rank(npc_id, player_id):
    """
    Calculate the global rank of a player based on their total loot from a specific NPC.
    :param npc_id: ID of the NPC
    :param player_id: ID of the player
    :return: The player's global rank
    """
    redis_key = f"npc:{npc_id}:global_loot_ranking"
    
    # Use Redis ZREVRANK to calculate rank based on total loot, with highest loot getting rank 1
    global_rank = redis_client.client.zrevrank(redis_key, player_id)
    
    if global_rank is None:
        return None
    
    # Redis ZREVRANK is zero-indexed, so add 1 to make it 1-indexed
    return global_rank + 1


def calculate_clan_npc_rank(npc_id, player_id, total_loot, clan_player_ids):
    """
    Dynamically calculates the clan loot ranking by filtering global rankings to include
    only clan members.
    :param npc_id: ID of the NPC
    :param player_id: ID of the player
    :param total_loot: Player's total loot value for the NPC
    :param clan_player_ids: List of player IDs who are part of the player's clan
    :return: The player's rank within the clan
    """
    clan_player_ids = [int(player_id) for player_id in clan_player_ids]
    redis_key = f"npc:{npc_id}:global_loot_ranking"
    
    # Fetch the global ranking (sorted by total loot in descending order)
    global_ranking = redis_client.client.zrevrange(redis_key, 0, -1, withscores=True)

    # Filter the global ranking to include only clan members
    clan_ranking = [(pid, loot) for pid, loot in global_ranking if int(pid) in clan_player_ids]

    # Calculate rank in the filtered clan list
    for rank, (pid, loot) in enumerate(clan_ranking, start=1):
        if int(pid) == player_id:
            return rank

    return None  # Player not found in the clan ranking


def get_npc_ids_from_target_list():
    npc_dict = {}
    for npc_name in target_list:
        npc_names = [npc_name]
        if npc_name == "Callisto":
            npc_names.append("Artio")
        elif npc_name == "Dagganoth Kings":
            npc_names.remove("Dagganoth Kings")
            npc_names.append("Dagganoth Rex")
            npc_names.append("Dagganoth Prime")
            npc_names.append("Dagganoth Supreme")
        elif npc_name == "Phosani's Nightmare":
            npc_names.append("Nightmare")
        elif npc_name == "Venenatis":
            npc_names.append("Spindel")
        elif npc_name == "Vet'ion":
            npc_names.append("Calvar'ion")
        elif npc_name == "The Gauntlet":
            npc_names.append("The Corrupted Gauntlet")
        for name in npc_names:
            npc: NpcList = sesh.query(NpcList.npc_id).filter(NpcList.npc_name == name).first()
            if npc:
                if npc_name not in npc_dict:
                    npc_dict[npc_name] = [npc.npc_id]
                else:
                    npc_dict[npc_name].append(npc.npc_id)
        
    return npc_dict 

def update_global_rank(npc_id, player_id, total_loot):
    """
    Updates the global loot ranking for a player in Redis.
    :param npc_id: ID of the NPC
    :param player_id: ID of the player
    :param total_loot: Player's total loot value for the NPC
    """
    redis_key = f"npc:{npc_id}:global_loot_ranking"
    
    # Add the player to the global ranking with their total loot as the score
    redis_client.client.zadd(redis_key, {player_id: total_loot})

def update_clan_rank(npc_id, partition, player_id, total_loot, clan_id):
    """
    Updates the clan loot ranking for a player in Redis.
    :param npc_id: ID of the NPC
    :param partition: The current partition (e.g., YYYYMM for monthly rankings)
    :param player_id: ID of the player
    :param total_loot: Player's total loot value for the NPC
    :param clan_id: Clan ID to scope the rankings to
    """
    redis_key = f"clan:{clan_id}:{partition}:{npc_id}:clan_loot_ranking"
    
    # Add the player to the clan ranking with their total loot as the score
    redis_client.client.zadd(redis_key, {player_id: total_loot})


