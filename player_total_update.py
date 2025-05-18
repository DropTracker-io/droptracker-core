### This separate process is used to run player update cycles in the background, 
# as opposed to holding up the main process's ability to respond to requests, etc.

import asyncio
import time
import aiohttp
import quart
from quart import Quart, request
import os
from dotenv import load_dotenv
import logging

from sqlalchemy import func
from db.models import Group, LBUpdate, session, Player, User, Drop, Session
# from db.update_player_total import update_player_in_redis
from db.update_player_total import update_player_in_redis
from lootboard.generator import generate_server_board
from utils.github import GithubPagesUpdater

from utils.redis import redis_client

from db.app_logger import AppLogger

app_logger = AppLogger()

# Dictionary to track recently updated players: {player_id: timestamp}
recently_updated = {}
# Cooldown period in seconds (60 minutes)
UPDATE_COOLDOWN = 3600

# Configure logging
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Create the Quart application
app = Quart(__name__)


def delete_player_keys(player_id, batch_size=100):
    """
    Delete player keys in batches to avoid blocking.
    """
    pattern = f"player:{player_id}:*"
    keys = []
    for key in redis_client.client.scan_iter(pattern, count=batch_size):
        keys.append(key)
        if len(keys) >= batch_size:
            redis_client.client.delete(*keys)
            keys = []
    if keys:
        redis_client.client.delete(*keys)   


# Define routes
@app.route('/')
async def index():
    return "Player Update Service is running!"

@app.route('/health')
async def health_check():
    return {"status": "healthy"}

async def send_top_npc_request(player_id):
    print("Skipping npc request to the api...")
    return True
    # url = f"https://www.droptracker.io/players/top-ranks-request/{player_id}"
    # print(f"Sending top NPC request to {url}")
    # async with aiohttp.ClientSession() as session:
    #     async with session.get(url) as response:
    #         if response.status == 200:
    #             data = await response.json()
    #             print(f"Got response: {data}")
    #             return True
    #         else:
    #             print(f"Failed response: {response.status}")
    #             return False
            
@app.route('/update', methods=['POST'])
async def update():
    data = await request.get_json()
    player_id = data.get('player_id')
    force_update = True
    print(f"Received update request for player {player_id}. Force update: {force_update}")
    # Check if player was recently updated
    current_time = time.time()
    if player_id in recently_updated:
        last_update = recently_updated[player_id]
        time_since_update = current_time - last_update
        
        if time_since_update < UPDATE_COOLDOWN:
            minutes_ago = int(time_since_update / 60)
            #app_logger.log(log_type="access", data=f"Skipping player {player_id} - updated {minutes_ago} minutes ago (cooldown: 60 minutes)", app_name="player_updates", description="update")
            return {"status": "skipped", "reason": f"Updated {minutes_ago} minutes ago"}
    # try:
    #     updated_top_npcs = await send_top_npc_request(player_id)
    #     if updated_top_npcs:
    #         logger.info(f"Updated top NPCs for player {player_id}")
    #     else:
    #         logger.info(f"Failed to update top NPCs for player {player_id}")
    # except Exception as e:
    #     logger.error(f"Error updating top NPCs for player {player_id}: {e}", exc_info=True)
    with Session() as session:
        try:
            print("Attempting to get player...")
            player = session.query(Player).filter(Player.player_id == player_id).first()
            if player:
                print("Player found, attempting to update...")
                ## Send the request to begin deleting keys first
                asyncio.create_task(asyncio.to_thread(delete_player_keys, player.player_id))
                player_drops = session.query(Drop).filter(Drop.player_id == player.player_id).all()
                updated = None
                # updated = update_player_in_redis(player.player_id, session, force_update=True, batch_drops=player_drops)
                print("Sending update")
                updated = update_player_in_redis(player.player_id, session, force_update=True, batch_drops=player_drops, from_submission=False)
                print("Returned:", updated)
                if updated and updated == True:
                    # Record the update time
                    recently_updated[player_id] = current_time
                    session.commit()
                    print("Updated player properly.")
                    #app_logger.log(log_type="access", data=f"Completed player update for player {player_id}", app_name="player_updates", description="update")
                    return {"status": "updated"}
                else:
                    print("Didn't update player properly.")
                    return {"status": "failed"}
            else:
                print("Player not found.")
                return {"status": "player not found"}
        except Exception as e:
            session.rollback()
            #app_logger.log(log_type="error", data=f"DB error: {e}", app_name="player_updates", description="update")
            return {"status": "failed"}
    

async def github_update_loop():
    updater = GithubPagesUpdater()
    #app_logger.log(log_type="access", data=f"Started GitHub update loop", app_name="player_updates", description="github_update_loop")
    while True:
        await updater.update_github_pages()
        await asyncio.sleep(3600)

# Background task for player updates
@app.before_serving
async def setup_background_tasks():
    app.cleanup_task = asyncio.create_task(cleanup_cache_loop())
    app.github_task = asyncio.create_task(github_update_loop())
    app_logger.log(log_type="access", data=f"Started background tasks", app_name="player_updates", description="setup_background_tasks")

async def get_all_groups(session_to_use = None):
    if session_to_use is not None:
        session = session_to_use
    groups = session.query(Group).all()
    return groups

async def lootboard_update_loop():
    return
#     app_logger.log(log_type="access", data=f"Started lootboard update loop", app_name="player_updates", description="lootboard_update_loop")
#     while True:
#         try:
#             await update_board()
#         except Exception as e:
#             print(f"Exception in lootboard_update_loop: {e}")
#             # Optionally, re-raise if you want the app to crash
#             # raise
#         finally:
#             await asyncio.sleep(120)

# async def update_board():
#     with Session() as session:
#         #print("Got session, attempting to get groups to use...")
#         groups = await get_all_groups(session)
#         #print("Got groups, attempting to generate boards...")
#         for group in groups:
#             #print(f"Updating board for group {group.group_id}")
#             if not os.path.exists(f"/store/droptracker/disc/static/assets/img/clans/{group.group_id}/lb"):
#                 os.makedirs(f"/store/droptracker/disc/static/assets/img/clans/{group.group_id}/lb")
#             try:#print("Generating board...")
#                 new_path = await generate_server_board(group_id=group.group_id, wom_group_id=group.wom_id, session_to_use=session)
#                 #print(f"Board has been generated: {new_path}")
#             #print("Board generated")
#             except Exception as e:
#                 print(f"Error generating board for group {group.group_id}: {e}")
#                 continue
#         #app_logger.log(log_type="info", data=f"Completed lootboard update loop. Waiting 2 minutes to continue", app_name="player_updates", description="lootboard_update_loop")


async def cleanup_cache_loop():
    """Background task to clean up the recently_updated cache"""
    try:
        while True:
            current_time = time.time()
            cleanup_threshold = current_time - (UPDATE_COOLDOWN * 2)
            
            # Find expired entries
            expired_keys = [player_id for player_id, timestamp in recently_updated.items() 
                           if timestamp < cleanup_threshold]
            
            # Remove expired entries
            for player_id in expired_keys:
                del recently_updated[player_id]
            
            if expired_keys:
                app_logger.log(log_type="access", data=f"Cleaned up {len(expired_keys)} expired cache entries", app_name="player_updates", description="cleanup_cache_loop")
            
            # Run cleanup every hour
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        app_logger.log(log_type="access", data=f"Cache cleanup loop is shutting down", app_name="player_updates", description="cleanup_cache_loop")
        raise
    except Exception as e:
        app_logger.log(log_type="error", data=f"Error in cache cleanup loop: {e}", app_name="player_updates", description="cleanup_cache_loop")

@app.after_serving
async def cleanup_background_tasks():
    app.cleanup_task.cancel()
    try:
        await app.cleanup_task
    except asyncio.CancelledError:
        app_logger.log(log_type="access", data=f"Background tasks were cancelled", app_name="player_updates", description="cleanup_background_tasks")

if __name__ == '__main__':
    # Get port from environment variable or use default
    port = int(os.getenv("PLAYER_UPDATE_PORT", 21475))
    
    # Run the Quart application
    app_logger.log(log_type="access", data=f"Starting Player Update Service on port {port}", app_name="player_updates", description="main")
    app.run(host='0.0.0.0', port=port)

