## Libraries
from typing import List
import uuid
from db.ops import associate_player_ids
from quart import Quart, request, jsonify, abort
from quart_jwt_extended import JWTManager, jwt_required, create_access_token
from quart_rate_limiter import RateLimiter, rate_limit
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
from sqlalchemy import desc, func, create_engine, inspect, or_, text
from sqlalchemy.orm import sessionmaker, scoped_session
from contextlib import contextmanager
import time
from collections import defaultdict, deque
import threading
import asyncio
import pymysql
from quart_cors import cors, route_cors
from urllib.parse import quote
# from new_generator_optimized import OptimizedRedisLootboardGenerator
from services.points import get_player_lifetime_points_earned
from utils.format import convert_from_ms, format_number
from utils.redis import RedisClient, get_true_player_total
from services.redis_updates import get_player_current_month_total, get_player_list_loot_sum

from data.TOP_NPCS import TOP_NPCS

redis_client = RedisClient()

## Core package dependencies
from data import submissions
from data.submissions import ca_processor, clog_processor, drop_processor, pb_processor
# Import session from db.models to avoid conflicts
from db.models import CombatAchievementEntry, Drop, GroupConfiguration, NotifiedSubmission, Session, get_current_partition, session, Player, Group, CollectionLogEntry, PersonalBestEntry, PlayerPet, ItemList, NpcList, engine

from utils.redis import RedisClient, calculate_rank_amongst_groups
from utils.wiseoldman import fetch_group_members
from worker import create_blueprint

## API Packages
from api.services.metrics import MetricsTracker

from utils.download import download_image, download_player_image

# Load environment variables
load_dotenv()

import logging

# Configure logging to suppress HTTP access logs  
logging.getLogger('quart.serving').setLevel(logging.ERROR)
#logging.getLogger('hypercorn.access').setLevel(logging.ERROR)
logging.getLogger('hypercorn.access').setLevel(logging.CRITICAL + 1)
logging.getLogger('hypercorn.access').disabled = True

# Initialize Quart app
app = Quart(__name__)
# Register the blueprint
app.register_blueprint(create_blueprint(), url_prefix='/')
rate_limiter = RateLimiter(app)

# Configure app
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_TOKEN_KEY")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=1)
jwt = JWTManager(app)

# Use the existing session factory from db.models instead of creating a new one
def get_db_session():
    """Get a fresh session using the existing session factory"""
    return Session()

# Function to reset all connections
def reset_db_connections():
    """Reset database connections using the existing session factory"""
    session.remove()

# Initialize metrics tracker
metrics = MetricsTracker()

@app.route("/submit", methods=["POST"])
@rate_limit(limit=10,period=timedelta(seconds=1))
async def submit_data():
    return await webhook_data()
    
@app.errorhandler(404)
async def not_found(e):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(500) 
async def server_error(e): 
    return jsonify({"error": "Internal server error"}), 500


@app.route("/ping")
async def ping():
    return jsonify({"message": "Pong"}), 200

@app.post("/check")
async def check():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415
    try:
        data = await request.get_json()
        incoming_guid = data.get("uuid")
        if not incoming_guid:
            return jsonify({"error": "Missing 'uuid'"}), 422

        print(f"Checking for guid: {incoming_guid}")

        # Run blocking DB lookups in a background thread to avoid stalling the event loop
        def find_entry(guid: str):
            session_local = get_db_session()
            try:
                entry = session_local.query(Drop).filter(Drop.unique_id == guid,
                                                        Drop.used_api == True,
                                                        Drop.date_added > datetime.now() - timedelta(hours=12)).first()
                if entry:
                    return entry, "drop"
                entry = session_local.query(CollectionLogEntry).filter(CollectionLogEntry.unique_id == guid,
                                                                        CollectionLogEntry.used_api == True,
                                                                        CollectionLogEntry.date_added > datetime.now() - timedelta(hours=12)).first()
                if entry:
                    return entry, "collection_log"
                entry = session_local.query(PersonalBestEntry).filter(PersonalBestEntry.unique_id == guid,
                                                                        PersonalBestEntry.used_api == True,
                                                                        PersonalBestEntry.date_added > datetime.now() - timedelta(hours=12)).first()
                if entry:
                    return entry, "personal_best"
                entry = session_local.query(CombatAchievementEntry).filter(CombatAchievementEntry.unique_id == guid,
                                                                            CombatAchievementEntry.used_api == True,
                                                                            CombatAchievementEntry.date_added > datetime.now() - timedelta(hours=12)).first()
                if entry:
                    return entry, "combat_achievement"
                return None, None
            finally:
                try:
                    session_local.close()
                except:
                    pass

        try:
            db_entry, entry_type = await asyncio.wait_for(
                asyncio.to_thread(find_entry, incoming_guid), timeout=3.0
            )
        except asyncio.TimeoutError:
            print(f"/check lookup timed out for guid: {incoming_guid}")
            return jsonify({
                "processed": False,
                "status": "timeout",
                "uuid": incoming_guid
            }), 200

        if not db_entry:
            print("No database entry found for guid: " + str(incoming_guid))
            return jsonify({
                "processed": False,
                "status": "not_found",
                "uuid": incoming_guid
            }), 200

        # Return a minimal, serializable payload (avoid returning ORM objects)
        payload = {"processed": True, "status": "processed", "uuid": incoming_guid, "type": entry_type}
        print("Returning payload: " + str(payload))
        if entry_type == "drop":
            payload["id"] = getattr(db_entry, "drop_id", None)
        elif entry_type == "collection_log":
            payload["id"] = getattr(db_entry, "log_id", None)
        elif entry_type == "personal_best":
            payload["id"] = getattr(db_entry, "id", None)
        elif entry_type == "combat_achievement":
            payload["id"] = getattr(db_entry, "id", None)

        return jsonify(payload), 200
    except Exception as e:
        print(f"/check error: {e}")
        return jsonify({"error": "Malformed or invalid request"}), 400
    finally:
        # No outer DB session is used in /check now
        pass

@app.route("/player_search", methods=["GET"])
async def player_search():
    """Search for a player by name"""
    player_name = request.args.get("name", None)
    if not player_name:
        return jsonify({"error": "Player name is required"}), 400
    
    db_session = get_db_session()
    try:
        # Search for player by name
        ## First filter by exact match
        player = db_session.query(Player).filter(Player.player_name == player_name).first()
        if not player:
            ## Then filter by partial match
            player = db_session.query(Player).filter(Player.player_name.ilike(f"%{player_name}%")).first()
        
        if not player:
            return jsonify({"error": f"Player '{player_name}' not found"}), 404
        
        player_recent_submissions = db_session.query(NotifiedSubmission).filter(or_(NotifiedSubmission.pb_id != None, NotifiedSubmission.drop_id != None, NotifiedSubmission.clog_id != None)).filter(NotifiedSubmission.player_id == player.player_id).order_by(NotifiedSubmission.date_added.desc()).limit(10).all()
        #print("Raw submissions:", len(player_recent_submissions))
        final_submission_data = await assemble_submission_data(player_recent_submissions, db_session)
        #print("Player final submission data:", final_submission_data)
        player_loot = redis_client.client.zscore(
            f"leaderboard:{get_current_partition()}",
            player.player_id
        )
        player_rank = redis_client.client.zrevrank(
            f"leaderboard:{get_current_partition()}",
            player.player_id
        )
        if player_rank is not None:
            player_rank += 1  # Convert from 0-indexed to 1-indexed
        #print("Player loot:", player_loot)
        player_npc_ranks = {}
        target_npcs = db_session.query(NpcList).filter(NpcList.npc_id.in_(TOP_NPCS)).all()
        for npc in target_npcs:
            player_npc_rank, player_npc_score = player.get_score_at_npc(npc.npc_id)
            player_npc_ranks[npc.npc_name] = {"rank": player_npc_rank, "loot": format_number(player_npc_score)}
        top_npc_name = max(player_npc_ranks, key=lambda x: player_npc_ranks[x]["loot"]) 
        top_npc_data = player_npc_ranks[top_npc_name].copy()
        top_npc_data["name"] = top_npc_name  # Add the NPC name to the data
        player_group_id_query = """SELECT group_id FROM user_group_association WHERE player_id = :player_id"""
        player_group_ids_result = db_session.execute(text(player_group_id_query), {"player_id": player.player_id}).fetchall()
        player_group_ids = [g[0] for g in player_group_ids_result if g[0] > 2] 
        player_groups = []
        for gid in player_group_ids:
            group_object: Group = db_session.query(Group).filter(Group.group_id == gid).first()
            player_groups.append({"name": group_object.group_name, 
                                  "id": gid,
                                  "loot": format_number(group_object.get_current_total()),
                                  "members": group_object.get_player_count(session_to_use=db_session)})
        player_groups.sort(key=lambda x: x["loot"], reverse=True)
        player_lifetime_points = get_player_lifetime_points_earned(player_id=player.player_id,session=db_session)
        # Sample data for now - replace with actual implementations later
        response_data = {
            "player_name": player.player_name,
            "droptracker_player_id": player.player_id,
            "registered": True if player.user_id else False,  # Assuming if player exists in DB, they're registered
            "total_loot": format_number(player_loot),
            "global_rank": player_rank,
            "top_npc": top_npc_data,
            "best_pb_rank": 42,
            "points": player_lifetime_points,
            "groups": player_groups,
            "recent_submissions": final_submission_data,
        }
        
        return jsonify(response_data), 200
    finally:
        db_session.close()

@app.route("/top_npcs", methods=["GET"])
async def top_npcs():
    """Get the top npcs"""
    db_session = get_db_session()
    try:
        npcs = db_session.query(NpcList).all()
        npcs_by_loot = []
        response_data = {}
        
        return jsonify(response_data), 200
        
    except Exception as e:
        print(f"Error in player_search: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        db_session.close()
    
@app.route("/top_players", methods=["GET"])
async def top_players():
    """Get the top players"""
    db_session = get_db_session()
    try:
        top_players = redis_client.client.zrevrange(
            f"leaderboard:{get_current_partition()}",
            0,
            4,
            withscores=True
        )
        #print(f"Got top player data -- partition {get_current_partition()} -- {top_players}")
        
        top_players_data = []
        for rank, (player_id, player_score) in enumerate(top_players, start=1):
            player = db_session.query(Player).filter(Player.player_id == player_id).first()
            top_players_data.append({
                "rank": rank,
                "player_name": player.player_name,
                "total_loot": format_number(player_score)
            })
        #print(f"Got top player data -- partition {get_current_partition()} -- {top_players_data}")
        return jsonify({"players": top_players_data}), 200  
    except Exception as e:
        #print(f"Error in top_players: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        db_session.close()

@app.route("/load_config", methods=["GET"])
async def load_config():
    """Load the config"""
    player_name = request.args.get("player_name", None)
    acc_hash = request.args.get("acc_hash", None)
    if not player_name or not acc_hash:
        return jsonify({"error": "Player name and acc_hash are required"}), 400
    db_session = get_db_session()
    if player_name == "Ra ine":
        print("Loading config for player:", player_name, acc_hash)
    try:
        player = db_session.query(Player).filter(Player.player_name == player_name, Player.account_hash == acc_hash).first()
        if not player:
            return jsonify({"error": "Player not found"}), 404
        player_gids = db_session.execute(text("SELECT group_id FROM user_group_association WHERE player_id = :player_id"), {"player_id": player.player_id}).all()
        group_configs = []
        def get_config_value(group_configs: List[GroupConfiguration], key: str):
            for group_config in group_configs:
                if group_config.config_key == key:
                    if key == "level_minimum_for_notifications":
                        return group_config.config_value
                    config_val = group_config.config_value if group_config.config_value and group_config.config_value != "" else group_config.long_value
                    if config_val == "true" or config_val == "1":
                        return True
                    elif config_val == "false" or config_val == "0":
                        return False
                    elif config_val == "":
                        return None
                    return config_val
            return ""
        ## TODO -- do not hardcore group ids here
        #player_gids = [(2, 0), (7, 0), (8, 0), (10, 0), (14, 0), (31, 0)]
        if player_name == "Ra ine":
            print("Got player gids:", player_gids)
        for group_id in player_gids:
            group_id = group_id[0]
            group = db_session.query(Group).filter(Group.group_id == group_id).first()
            current_group_configs = db_session.query(GroupConfiguration).filter(GroupConfiguration.group_id == group_id).all()
            group_configs.append({"group_id": group_id,
                                "group_name": group.group_name,
                                "min_value": get_config_value(current_group_configs, "minimum_value_to_notify"),
                                "minimum_drop_value": get_config_value(current_group_configs, "minimum_value_to_notify"),
                                "only_screenshots": get_config_value(current_group_configs, "only_send_messages_with_images"),
                                "send_drops": True,
                                "send_pbs": get_config_value(current_group_configs, "notify_pbs"),
                                "send_clogs": get_config_value(current_group_configs, "notify_clogs"),
                                "send_cas": get_config_value(current_group_configs, "notify_cas"),
                                "send_pets": get_config_value(current_group_configs, "send_pets"),
                                "send_xp": get_config_value(current_group_configs, "notify_levels"),
                                "minimum_level": get_config_value(current_group_configs, "level_minimum_for_notifications"),
                                "send_stacked_items": get_config_value(current_group_configs, "send_stacks_of_items"),
                                "minimum_ca_tier": get_config_value(current_group_configs, "min_ca_tier_to_notify")})
        
        if player_name == "Ra ine":
            print("Group configs:", group_configs)
        return jsonify(group_configs), 200
    finally:
        db_session.close()

@app.route("/top_groups", methods=["GET"])
async def top_groups():
    """Get the top groups"""
    db_session = get_db_session()
    try:
        groups = db_session.query(Group).all()
    

        group_totals = {}  # Dictionary to store total loot by group_id
        partition = datetime.now().year * 100 + datetime.now().month

        for group_object in groups:
            group_id = group_object.group_id  # Extract the group_id
            if group_id == 2 or group_id == 0:
                ## Do not track the global group in ranking listings
                continue
            # print("Group ID from database:", group)
            # Query all players in this group
            players_in_group = db_session.query(Player.player_id).join(Player.groups).filter(Group.group_id == group_id).all()

            # Initialize group total
            group_totals[group_id] = 0

            # Fetch each player's total loot from Redis
            try:
                group_month_total = get_player_list_loot_sum([player.player_id for player in players_in_group])
                group_totals[group_id] = group_month_total
            except Exception as e:
                print(f"Error getting group total for group {group_id}: {e}")
                group_totals[group_id] = 0

        # Sort groups by total loot, descending
        sorted_groups = sorted(group_totals.items(), key=lambda x: x[1], reverse=True)
        total_groups = len(sorted_groups)
        # Find the rank of the passed group_id
        final_groups = []
        for rank, (g_id, group_total) in enumerate(sorted_groups, start=1):
            group_object = db_session.query(Group).filter(Group.group_id == g_id).first()
            if g_id != 2 and g_id != 0:
                top_player_data = redis_client.client.zrevrange(
                    f"leaderboard:{get_current_partition()}:group:{g_id}",
                    0,
                    0,
                    withscores=True
                )

                top_player_display = None
                if top_player_data:
                    # zrevrange returns a list of (member, score) tuples when withscores=True
                    player_id_raw, player_score = top_player_data[0]

                    # Decode bytes -> str -> int as needed
                    try:
                        player_id_int = int(player_id_raw.decode("utf-8")) if isinstance(player_id_raw, (bytes, bytearray)) else int(player_id_raw)
                    except Exception:
                        player_id_int = int(player_id_raw)

                    top_player: Player | None = db_session.query(Player).filter(Player.player_id == player_id_int).first()

                    if top_player:
                        top_player_display = f"{top_player.player_name}"

                final_groups.append({
                    "group_name": group_object.group_name,
                    "total_loot": format_number(group_total),
                    "rank": rank,
                    "group_id": group_object.group_id,
                    "member_count": len(players_in_group),
                    "top_player": top_player_display,
                })
        return jsonify({"groups": final_groups}), 200
    finally:
        db_session.close()

    return jsonify({"message": "Top groups"}), 200

@app.route("/group_search", methods=["GET"])
async def group_search():
    """Search for a group by name"""
    group_name = request.args.get("name", None)
    if not group_name:
        return jsonify({"error": "Group name is required"}), 400

    db_session = get_db_session()
    try:
        group: Group = db_session.query(Group).filter(Group.group_name == group_name).first()
        if not group:
            return jsonify({"error": "Group " + group_name + " not found"}), 404
        from db.ops import calculate_rank_amongst_groups
        group_wom_id = db_session.query(Group.wom_id).filter(Group.group_id == group.group_id).first()
        try:    
            if group_wom_id:
                group_wom_id = group_wom_id[0]
            if group_wom_id:
                print("Finding group members?")
                wom_member_list = await fetch_group_members(wom_group_id=int(group_wom_id), session_to_use=db_session)
        except Exception as e:
            print("Couldn't get the member list", e)
            return
        #print("Got wom member list")
        player_ids = await associate_player_ids(wom_member_list,session_to_use=db_session)
        group_rank, total_groups = calculate_rank_amongst_groups(group.group_id, player_ids, session_to_use=db_session)
        top_player_data = redis_client.client.zrevrange(
            f"leaderboard:{get_current_partition()}:group:{group.group_id}",
            0,
            0,
            withscores=True
        )

        top_player_display = None
        if top_player_data:
            # zrevrange returns a list of (member, score) tuples when withscores=True
            player_id_raw, player_score = top_player_data[0]

            # Decode bytes -> str -> int as needed
            try:
                player_id_int = int(player_id_raw.decode("utf-8")) if isinstance(player_id_raw, (bytes, bytearray)) else int(player_id_raw)
            except Exception:
                player_id_int = int(player_id_raw)

            top_player: Player | None = db_session.query(Player).filter(Player.player_id == player_id_int).first()

            if top_player:
                top_player_display = f"{top_player.player_name}"

        player_count = group.get_player_count(session_to_use=db_session)

        group_recent_submissions = db_session.query(NotifiedSubmission).filter(or_(NotifiedSubmission.pb_id != None, NotifiedSubmission.drop_id != None, NotifiedSubmission.clog_id != None)).filter(NotifiedSubmission.group_id == group.group_id).order_by(NotifiedSubmission.date_added.desc()).limit(10).all()
        final_submission_data = await assemble_submission_data(group_recent_submissions, db_session)
        partition = get_current_partition()
        players_in_group = db_session.query(Player.player_id).join(Player.groups).filter(Group.group_id == group.group_id).all()
        group_total = get_player_list_loot_sum([player.player_id for player in players_in_group])
        print("Group total loot is:", group_total)
        return jsonify({
            "group_name": group.group_name,
            "group_description": group.description,
            "group_image_url": group.icon_url,
            "public_discord_link": group.invite_url if group.invite_url else None,
            "group_droptracker_id": group.group_id,
            "group_members": player_count,
            "group_rank": f"{group_rank}/{total_groups}",
            "group_top_player": top_player_display,
            "group_recent_submissions": final_submission_data,
            "group_stats": {
                "total_members": player_count,
                "global_rank": f"{group_rank}/{total_groups}",
                "monthly_loot": format_number(group_total),
            },
        })
    finally:
        db_session.close()

async def assemble_submission_data(submissions, db_session = None):
    if not db_session:
        db_session = get_db_session()
    """Assemble submission data for a group"""
    final_submission_data = []
    drops = []
    pbs = []
    clogs = []
    try:
        for submission in submissions:
            submission_data = {}
            submission_data["date_received"] = submission.date_added.isoformat()
            player = db_session.query(Player).filter(Player.player_id == submission.player_id).first()
            submission_data["player_name"] = player.player_name
            if submission.pb_id:    
                sub_type = "pb"
            elif submission.drop_id:
                sub_type = "drop"
            elif submission.clog_id:
                sub_type = "clog"
            else:
                continue
            submission_data["submission_type"] = sub_type
            match (sub_type):
                case "pb":
                    if submission.pb_id not in pbs:
                        pbs.append(submission.pb_id)
                    else:
                        continue
                    pb_entry: PersonalBestEntry = db_session.query(PersonalBestEntry).filter(PersonalBestEntry.id == submission.pb_id).first()
                    raw_best_time = pb_entry.personal_best if (pb_entry.personal_best > 0 and pb_entry.personal_best < pb_entry.kill_time) else pb_entry.kill_time if pb_entry.kill_time > 0 else 0
                    if raw_best_time == 0:
                        continue
                    npc = db_session.query(NpcList).filter(NpcList.npc_id == pb_entry.npc_id).first()
                    submission_data["display_name"] = "Personal Best: " + convert_from_ms(raw_best_time)
                    submission_data["image_url"] = f"https://www.droptracker.io/img/npcdb/{pb_entry.npc_id}.png"
                    submission_data["source_name"] = npc.npc_name
                    if pb_entry.image_url:
                        submission_img_url = str(pb_entry.image_url)
                        submission_data["submission_image_url"] = submission_img_url.replace("/store/droptracker/disc/static/assets/img/", "https://www.droptracker.io/img/")
                        # URL encode the image URL to handle special characters
                        submission_data["submission_image_url"] = quote(submission_data["submission_image_url"], safe=':/?#[]@!$&\'()*+,;=')
                    submission_data["data"] = [
                        {
                            "type": "best_time",
                            "time": convert_from_ms(raw_best_time),
                            "team_size": pb_entry.team_size,
                        }
                    ]
                case "drop":
                    if submission.drop_id not in drops:
                        drops.append(submission.drop_id)
                    else:
                        continue
                    drop_entry: Drop = db_session.query(Drop).filter(Drop.drop_id == submission.drop_id).first()
                    item = db_session.query(ItemList).filter(ItemList.item_id == drop_entry.item_id).first()
                    submission_data["image_url"] = f"https://www.droptracker.io/img/itemdb/{item.item_id}.png"
                    npc = db_session.query(NpcList).filter(NpcList.npc_id == drop_entry.npc_id).first()
                    submission_data["source_name"] = npc.npc_name
                    submission_data["display_name"] = f"{item.item_name}"
                    drop_value = drop_entry.value * drop_entry.quantity
                    submission_data["value"] = format_number(drop_value)
                    if drop_entry.image_url:
                        submission_img_url = str(drop_entry.image_url)
                        submission_data["submission_image_url"] = submission_img_url.replace("/store/droptracker/disc/static/assets/img/", "https://www.droptracker.io/img/")
                        # URL encode the image URL to handle special characters
                        submission_data["submission_image_url"] = quote(submission_data["submission_image_url"], safe=':/?#[]@!$&\'()*+,;=')
                    submission_data["data"] = [
                        {
                            "type": "item",
                            "value": format_number(drop_value),
                            "name": item.item_name,
                            "id": item.item_id,
                        }
                    ]
                case "clog":
                    if submission.clog_id not in clogs:
                        clogs.append(submission.clog_id)
                    else:
                        continue
                    clog_entry: CollectionLogEntry = db_session.query(CollectionLogEntry).filter(CollectionLogEntry.log_id == submission.clog_id).first()
                    item = db_session.query(ItemList).filter(ItemList.item_id == clog_entry.item_id).first()
                    submission_data["image_url"] = f"https://www.droptracker.io/img/itemdb/{item.item_id}.png"
                    submission_data["source_name"] = item.item_name
                    submission_data["display_name"] = f"{item.item_name}"
                    if clog_entry.image_url:
                        submission_img_url = str(clog_entry.image_url)
                        submission_data["submission_image_url"] = submission_img_url.replace("/store/droptracker/disc/static/assets/img/", "https://www.droptracker.io/img/")
                        # URL encode the image URL to handle special characters
                        submission_data["submission_image_url"] = quote(submission_data["submission_image_url"], safe=':/?#[]@!$&\'()*+,;=')
                    submission_data["data"] = [
                        {
                            "type": "clog_item",
                            "name": item.item_name,
                            "id": item.item_id,
                        }
                    ]
            final_submission_data.append(submission_data)
    except Exception as e:
        print(f"Error assembling submission data: {e}")
        return []
    finally:
        if db_session:
            db_session.close()
    return final_submission_data

@app.route("/player", methods=["GET"])
@rate_limit(limit=5,period=timedelta(seconds=10))
async def get_player():
    """Get player data"""
    player_name = request.args.get("player_name")
    if not player_name:
        return jsonify({"error": "Player name is required"}), 400
    
    # Use a proper session scope
    db_session = get_db_session()
    try:
        player = db_session.query(Player).filter(Player.player_name == player_name).first()
        if not player:
            return jsonify({"error": "Player not found"}), 404
        return jsonify({"player": player.to_dict()})
    finally:
        db_session.close()

@app.route("/metrics", methods=["GET"])
async def get_metrics():
    """Get current metrics"""
    return jsonify(metrics.get_stats())

@app.route("/latest_news", methods=["GET"])
async def get_latest_news():
    """Get the latest news"""
    return "API connected. We are having some issues tracking all submissions. Please report any issues you experience to !droptracker"



@app.route("/webhook", methods=["POST"])
@rate_limit(limit=100,period=timedelta(seconds=1))
async def webhook_data():
    """
    Handle Discord webhook-style messages and convert them to the standard format
    for processing by the existing processors.
    """
    success = False
    request_type = "webhook"
    submission_type = None
    db_session = None
    
    try:
        # Debug the raw request to see what's coming in
        content_type = request.headers.get('Content-Type', '')
        
        # Check if this is a multipart request
        if 'multipart/form-data' in content_type:
            try:
                # Get the boundary from the content type
                boundary = None
                for part in content_type.split(';'):
                    part = part.strip()
                    if part.startswith('boundary='):
                        boundary = part[9:].strip('"')
                        break
                
                
                # Get the raw request data
                body = await request.body
                body_str = body.decode('latin1')  # Use latin1 to handle binary data
                
                
                # Look for the file part in the raw body
                file_header = f'Content-Disposition: form-data; name="file"; filename="image.jpeg"'
                
                # Process the form data normally
                form = await request.form
                
                payload_json = form.get('payload_json')
                if not payload_json:
                    return jsonify({"error": "No payload_json found in form data"}), 400
                
                # Parse the JSON payload
                import json
                webhook_data = json.loads(payload_json)
                
                if webhook_data is None:
                    return jsonify({"error": "Invalid JSON in payload_json"}), 400
                
                #("Parsed webhook data:", webhook_data)
                
                # Try to get the file directly from the request files
                files = await request.files
                #print(f"Request files: {files}")
                
                # Handle image file if present
                image_file = None
                if 'file' in files:
                    image_file = files['file']
                    #print(f"Received image file: {image_file.filename}, "
                          #f"content_type: {image_file.content_type}")
                
                # Extract data from Discord webhook format
                processed_items = await process_webhook_data(webhook_data)
                
                if not processed_items:
                    return jsonify({"error": "Could not process webhook data"}), 400
                
                # Add image data to processed_data if available
                submission_type = processed_items[0].get("type")
                processed_items[0]["downloaded"] = False
                
                # Create a fresh database connection for each request
                db_session = get_db_session()
                response = None
                try:
                    downloaded = False
                    file_path = None
                    for processed_data in processed_items:
                        submission_type = processed_data.get("type")
                        processed_data["downloaded"] = False
                        
                        if image_file:
                            #print("Got image file in form data")
                            processed_data["has_image"] = True
                            # Use a separate session for image processing to avoid conflicts
                            image_session = get_db_session()
                            #print(f"Processed data: {processed_data}")
                            player_name = processed_data.get('player', processed_data.get('player_name', None))
                            #print(f"Player name from processed_data: {player_name}")
                            try:
                                player = image_session.query(Player).filter(Player.player_name == player_name).first()
                                #print(f"Found player: {player}")
                                player_wom_id = player.wom_id if player else None
                                if player:
                                    file_path = await download_image(sub_type=processed_data.get('type', 'unknown'), player=player, player_wom_id=player_wom_id, file_data=image_file, processed_data=processed_data)
                                    if file_path:
                                        # Prefer externally served URL if available
                                        if processed_data.get("image_path"):
                                            file_path = processed_data["image_path"]
                                        processed_data["image_url"] = file_path
                                        processed_data["downloaded"] = True
                                        downloaded = True
                                        #print(f"Successfully processed image, external URL: {processed_data['image_url']}")
                                    else:
                                        pass
                                        #print(f"Failed to process image for {processed_data.get('player', 'unknown player')}")
                            finally:
                                image_session.close()
                        else:
                            ## Add the downloaded image from this set of webhook data to each processed_data (individual drop/submission)
                            if file_path:
                                # Use the image_path (external URL) instead of file_path (local path)
                                if processed_data.get("image_path"):
                                    processed_data["image_url"] = processed_data["image_path"]
                                else:
                                    processed_data["image_url"] = file_path  # fallback to local path if external URL not available
                        processed_data['used_api'] = True
                        match (submission_type):
                            case "drop" | "other"| "npc":
                                submission_type = "drop"
                                response = await submissions.drop_processor(processed_data, external_session=db_session)
                            case "collection_log":
                                submission_type = "collection_log"
                                print(f"Processed data: {processed_data}")
                                await submissions.clog_processor(processed_data, external_session=db_session)
                            case "personal_best" | "kill_time" | "npc_kill":
                                submission_type = "personal_best"
                                #print("Got pb processed data: ", processed_data)
                                await submissions.pb_processor(processed_data, external_session=db_session)
                            case "combat_achievement":
                                submission_type = "combat_achievement"
                                await submissions.ca_processor(processed_data, external_session=db_session)
                            case "experience_update" | "experience_milestone" | "level_up":
                                #submission_type = "experience"
                                #await submissions.experience_processor(processed_data, external_session=db_session)
                                continue
                            case "quest_completion":
                                #submission_type = "quest_completion"
                                #await submissions.quest_processor(processed_data, external_session=db_session)
                                continue
                            case "pet":
                                await submissions.pet_processor(processed_data, external_session=db_session)
                                continue
                            case "adventure_log":
                                await submissions.adventure_log_processor(processed_data, external_session=db_session)
                                continue
                            case _:
                                #print(f"Unknown submission type: {submission_type} data: {processed_data}")
                                continue
                    
                    # Only commit if we successfully processed all items
                    db_session.commit()
                    success = True
                    
                except Exception as processor_error:
                    print(f"Processor error: {processor_error}")
                    # Roll back on error
                    if db_session:
                        db_session.rollback()
                    return jsonify({"error": f"Error processing data: {str(processor_error)}"}), 200
                if response:
                    return jsonify({"message": response.message, "notice": response.notice}), 200
                else:
                    return jsonify({"message": "Webhook data processed successfully"}), 200
                
            except Exception as e:
                print(f"Error processing multipart request: {e}")
                return jsonify({"error": f"Error processing request: {str(e)}"}), 400
        else:
            # Handle non-multipart requests (e.g., JSON)
            try:
                data = await request.get_json()
                # Process JSON data...
                return jsonify({"message": "JSON data processed"}), 200
            except Exception as e:
                print(f"Error processing JSON request: {e}")
                return jsonify({"error": f"Error processing request: {str(e)}"}), 400
    except Exception as e:
        print("Webhook Exception: ", e)
        return jsonify({"error": str(e)}), 500
    finally:
        # Always close the session if it was created
        if db_session:
            try:
                db_session.close()
            except:
                pass
        
        # Force cleanup of any lingering sessions on error
        if not success:
            reset_db_connections()
        
        # Record metrics regardless of success/failure
        if submission_type:
            metrics.record_request(submission_type, success, app="new_api")
        else:
            metrics.record_request(request_type, success, app="new_api")

async def process_webhook_data(webhook_data):
    """Process webhook data from Discord format to standard format"""
    try:
        # Extract the content and embeds from the webhook data
        embeds = webhook_data.get("embeds", [])
        
        if not embeds:
            print("No embeds found in webhook data")
            return None
        
        # Process all embeds and return a list of processed data
        processed_items = []
        for embed in embeds:
            # Extract fields from the embed and create the data structure
            processed_data = {
                field["name"]: field["value"] for field in embed.get("fields", [])
            }
            
            # Add timestamp
            processed_data["timestamp"] = datetime.now().isoformat()
            processed_items.append(processed_data)
        
        return processed_items
    except Exception as e:
        print(f"Error processing webhook data: {e}")
        return None


@app.route("/groups/board_update/<int:group_id>", methods=["GET"])
@route_cors(allow_origin="https://www.droptracker.io")
async def group_board_update(group_id: int):
    try:
        force_raw = request.args.get("force", "false").lower()
        force = force_raw in ("1", "true", "yes", "y", "on")

        import sys
        from asyncio.subprocess import PIPE

        cmd = [sys.executable, "/store/droptracker/disc/board_cli.py", str(group_id)]
        if force:
            cmd.append("--force")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=PIPE,
            stderr=PIPE
        )
        stdout, stderr = await proc.communicate()
        status_code = 200 if proc.returncode == 0 else 500

        return jsonify({
            "message": "Board update completed" if proc.returncode == 0 else "Board update failed",
            "group_id": group_id,
            "force": force,
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="ignore"),
            "stderr": stderr.decode(errors="ignore"),
        }), status_code
    except Exception as e:
        return jsonify({"error": f"Failed to run board update: {e}"}), 500


async def save_image(file_data, processed_data):
    """Save uploaded image to disk with a unique filename"""
    try:
        import os
        import uuid
        
        # Create directory if it doesn't exist
        upload_dir = os.path.join(os.getcwd(), "uploads")
        os.makedirs(upload_dir, exist_ok=True)
        
        # Generate unique filename
        filename = f"{uuid.uuid4()}.jpg"
        filepath = os.path.join(upload_dir, filename)
        
        # Save the file
        await file_data.save(filepath)
        
        # Add the filepath to the processed data
        processed_data["image_path"] = filepath
        
        print(f"Saved image to {filepath}")
        return filepath
    except Exception as e:
        print(f"Error saving image: {e}")
        return None

async def get_item_id_by_name(item_name):
    """Get item ID from name with robust connection handling"""
    for attempt in range(3):  # Try up to 3 times
        session = None
        try:
            session = get_db_session()
            item = session.query(ItemList.item_id).filter(
                ItemList.item_name.ilike(f"%{item_name}%")
            ).first()
            result = item[0] if item else 0
            session.close()
            return result
        except Exception as e:
            print(f"Database error in get_item_id_by_name (attempt {attempt+1}/3): {e}")
            if session:
                try:
                    session.rollback()
                    session.close()
                except:
                    pass
            
            if attempt == 2:  # Last attempt
                print(f"Failed to get item ID for '{item_name}' after 3 attempts")
                return 0
            await asyncio.sleep(0.5)  # Wait before retrying

async def get_npc_id_by_name(npc_name):
    """Get NPC ID from name with robust connection handling"""
    for attempt in range(3):  # Try up to 3 times
        session = None
        try:
            session = get_db_session()
            npc = session.query(NpcList.npc_id).filter(
                NpcList.npc_name.ilike(f"%{npc_name}%")
            ).first()
            result = npc[0] if npc else 0
            session.close()
            return result
        except Exception as e:
            print(f"Database error in get_npc_id_by_name (attempt {attempt+1}/3): {e}")
            if session:
                try:
                    session.rollback()
                    session.close()
                except:
                    pass
                
            if attempt == 2:  # Last attempt
                print(f"Failed to get NPC ID for '{npc_name}' after 3 attempts")
                return 0
            await asyncio.sleep(0.5)  # Wait before retrying

def convert_time_to_ms(time_str):
    """Convert time string (e.g. '1:23.45') to milliseconds"""
    try:
        if ":" in time_str:
            parts = time_str.split(":")
            if len(parts) == 2:
                minutes, seconds = parts
                return (int(minutes) * 60 + float(seconds)) * 1000
            elif len(parts) == 3:
                hours, minutes, seconds = parts
                return (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
        return int(float(time_str) * 1000)
    except:
        return 0


if __name__ == "__main__":
    import socket
    import time
    
    # Try to kill any process using the port
    import os
    os.system(f"kill $(lsof -t -i:31323) 2>/dev/null || true")
    
    # Wait a moment for the port to be released
    time.sleep(1)
    
    # Check if port is available before starting
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 31323))
        sock.close()
        print("Port 31323 is available, starting server...")
        app.run(host="127.0.0.1", port=31323)
    except OSError as e:
        print(f"Port 31323 is still in use. Error: {e}")
        print("Please manually check for processes using this port with:")
        print("sudo lsof -i:31323")
        print("Then kill the process with:")
        print("kill -9 <PID>")