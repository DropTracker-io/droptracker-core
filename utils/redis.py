# redis.py
import redis
from typing import Optional
from utils.format import normalize_npc_name
from datetime import datetime
from db.models import User, Group, Guild, Player, NpcList, ItemList, PersonalBestEntry, Drop, UserConfiguration, session, ItemList, GroupConfiguration
import os
from dotenv import load_dotenv

load_dotenv()
REDIS_PW = os.getenv('DB_PASS')
## Singleton RedisClient class
class RedisClient:
    _instance: Optional['RedisClient'] = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls, *args, **kwargs)
        return cls._instance
    
    def __init__(self, host: str = '127.0.0.1', port: int = 6379, db: int = 0):
        if not hasattr(self, 'client'):
            try:
                self.client = redis.Redis(host=host, port=port, db=db, password=REDIS_PW)
            except Exception as e:
                print(f"Error connecting to Redis: {e}")
                self.client = None

    def set(self, key: str, value: str) -> None:
        try:
            self.client.set(key, value)
        except redis.RedisError as e:
            print(f"Error setting key '{key}': {e}")

    def get(self, key: str) -> Optional[str]:
        try:
            value = self.client.get(key)
            return value.decode('utf-8') if value else None
        except redis.RedisError as e:
            print(f"Error getting key '{key}': {e}")
            return None

    def delete(self, key: str) -> None:
        try:
            self.client.delete(key)
        except redis.RedisError as e:
            print(f"Error deleting key '{key}': {e}")
    
    def decode_data(self, data):
        return {key.decode('utf-8'): value.decode('utf-8') for key, value in data.items()}

    def exists(self, key: str) -> bool:
        try:
            return self.client.exists(key)
        except redis.RedisError as e:
            print(f"Error checking existence of key '{key}': {e}")
            return False
        
redis_client = RedisClient()


def calculate_rank_amongst_groups(group_id, player_ids):
    # Get all groups except the passed one
    print("Calculating group rank amongst other groups based on total loot")
    groups = session.query(Group.group_id).all()
    
    # If there are no other groups, return rank 1/1
    if not groups:
        print("No other groups exist")
        return 1, 1

    group_totals = {}  # Dictionary to store total loot by group_id
    partition = datetime.now().year * 100 + datetime.now().month

    for group_tuple in groups:
        group = group_tuple[0]  # Extract the group_id
        if group == 2 or group == 0:
            ## Do not track the global group in ranking listings
            continue
        # print("Group ID from database:", group)
        # Query all players in this group
        players_in_group = session.query(Player.player_id).join(Player.groups).filter(Group.group_id == group).all()

        # Initialize group total
        group_totals[group] = 0

        # Fetch each player's total loot from Redis
        for player in players_in_group:
            player_total = get_true_player_total(player[0])
            if player_total:
                group_totals[group] += player_total  # Add player's total loot to group's total

    # Sort groups by total loot, descending
    sorted_groups = sorted(group_totals.items(), key=lambda x: x[1], reverse=True)
    total_groups = len(sorted_groups)
    # Find the rank of the passed group_id
    for rank, (g_id, group_total) in enumerate(sorted_groups, start=1):
        if g_id == group_id:
            print("This group is ranked", rank)
            return rank, total_groups
    
    return None, total_groups  # If group_id is not found



def calculate_global_overall_rank(player_id):
    """ Returns a tuple of the player's rank and the total number of players ranked 
    rank, total
    """
    partition = datetime.now().year * 100 + datetime.now().month
    player_totals = {}
    
    # Query all player IDs
    player_ids = session.query(Player.player_id).all()
    
    # Iterate through all players and get their total loot from Redis
    for player_tuple in player_ids:
        player = player_tuple[0]  # Extract the player_id from the tuple
        player_total = get_true_player_total(player)
        player_totals[player] = player_total
    total_ranked = len(player_totals)
    sorted_player_totals = sorted(player_totals.items(), key=lambda x: x[1], reverse=True)
    # Correct the loop for ranking
    for rank, (pid, loot) in enumerate(sorted_player_totals, start=1):
        if int(pid) == int(player_id):
            return rank, total_ranked
        
    return None, total_ranked

def calculate_clan_overall_rank(player_id, clan_player_ids):
    """
    Calculate the overall rank of a player in their clan based on other members
    using total loot gained this month
    """
    clan_player_ids = [int(player_id) for player_id in clan_player_ids]
    # print("Clan player IDs:", clan_player_ids)
    partition = datetime.now().year * 100 + datetime.now().month
    player_totals = {}
    group_total = 0
    
    for pid in clan_player_ids:
        player_total = get_true_player_total(pid)
        player_totals[pid] = player_total
        group_total += int(player_total)
    
    total_ranked = len(player_totals)
    if total_ranked < 1:
        total_ranked = 1
    if group_total < 1:
        group_total = 0
    sorted_player_totals = sorted(player_totals.items(), key=lambda x: x[1], reverse=True)
    # Corrected loop for ranking
    for rank, (pid, loot) in enumerate(sorted_player_totals, start=1):
        if int(pid) == player_id:
            return rank, total_ranked, group_total
    
    return 0, total_ranked, group_total

def get_true_player_total(player_id):
    """
    Get the true, most accurate player total from Redis
    """
    partition = datetime.now().year * 100 + datetime.now().month
    total_items_key = f"player:{player_id}:{partition}:total_items"
    # Get total items
    total_items = redis_client.client.hgetall(total_items_key)
    #print("redis update total items stored:", total_items)
    player_total = 0
    for key, value in total_items.items():
        key = key.decode('utf-8')
        value = value.decode('utf-8')
        try:
            quantity, total_value = map(int, value.split(','))
        except ValueError:
            #print(f"Error processing item {key} for player {player_id}: {value}")
            continue
        player_total += total_value
    return player_total