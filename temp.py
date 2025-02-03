import redis
import os
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from datetime import datetime
import json
from dataclasses import dataclass
import logging
from db.models import Drop, ItemList, Player, session
from sqlalchemy import text

load_dotenv()
class StandaloneRedisClient:
    _instance: Optional['StandaloneRedisClient'] = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, host: str = 'localhost', port: int = 6379, db: int = 0):
        if not hasattr(self, 'client'):
            redis_pw = os.getenv('BACKEND_ACP_TOKEN')
            self.client = redis.Redis(
                host=host, 
                port=port, 
                db=db
            )

    def get(self, key: str) -> Optional[str]:
        try:
            value = self.client.get(key)
            return value.decode('utf-8') if value else None
        except redis.RedisError as e:
            return None

def update_player_in_redis(player_id: int, force_update: bool, drops: List[Drop]) -> None:
    """Update the player's total loot and related data in Redis."""
    current_partition = datetime.now().year * 100 + datetime.now().month
    
    redis_client = StandaloneRedisClient()
    # Initialize Redis pipeline
    pipeline = redis_client.client.pipeline(transaction=False)
    
    # Track totals per partition
    partition_totals = {}
    all_time_totals = {
        'total_loot': 0,
        'items': {},
        'npcs': {}
    }
    print("Initialized empty dictionaries.")
    # Reference to original update_player_in_redis function
    # Lines 74-178 from update_player_total.py 
    if drops == [] or force_update == True:
        drops = session.query(Drop).filter(Drop.player_id == player_id).all()
    for drop in drops:
        ## Sort through the list of drops and update totals in redis
        drop_partition = drop.partition  # Use the drop's actual partition (i.e, 202501 for jan 2025.)
        
        if drop_partition not in partition_totals:
            ## If this is the first time we've seen this partition, add it with empty values to the dict
            partition_totals[drop_partition] = {
                'total_loot': 0,
                'items': {},
                'npcs': {}
            }
        ## determine the total value of the drop based on quantity * value
        total_value = drop.value * drop.quantity
        item_name = session.query(ItemList).filter(ItemList.item_id == drop.item_id, ItemList.noted == False).first()
        #print(f"Processing {drop.quantity} x {item_name} (value: {drop.value})")
        # Update partition totals
        #print("Total:", partition_totals[drop_partition]['total_loot'], "(before)")
        partition_totals[drop_partition]['total_loot'] += total_value
        #print("Total:", partition_totals[drop_partition]['total_loot'], "(after)")
        if drop.item_id not in partition_totals[drop_partition]['items']:
            ## Add the item to the items dict with an empty set of values if it doesn't exist
            partition_totals[drop_partition]['items'][drop.item_id] = [0, 0]  # [qty, value]
        #print("Item: ", partition_totals[drop_partition]['items'][drop.item_id], "(after)")
        ## Add the quantity and value to the item's dict
        #print("Item: ", partition_totals[drop_partition]['items'][drop.item_id], "(before)")
        partition_totals[drop_partition]['items'][drop.item_id][0] += drop.quantity
        partition_totals[drop_partition]['items'][drop.item_id][1] += total_value
        #print("Item:", partition_totals[drop_partition]['items'][drop.item_id], "(after)")
        
        # Update NPC totals for partition
        if drop.npc_id not in partition_totals[drop_partition]['npcs']:
            ## Add the npc to the npcs dict with an empty set of values if it doesn't exist
            partition_totals[drop_partition]['npcs'][drop.npc_id] = 0
        ## Add the total value to the npc's dict
        partition_totals[drop_partition]['npcs'][drop.npc_id] += total_value
        
        # Update all-time totals
        #print("All-time total:", all_time_totals['total_loot'], "(before)")
        all_time_totals['total_loot'] += total_value
        #print("All-time total:", all_time_totals['total_loot'], "(after)")
        if drop.item_id not in all_time_totals['items']:
            ## Add the item to the items dict with an empty set of values if it doesn't exist
            #print("First time this item has appeared.")
            all_time_totals['items'][drop.item_id] = [0, 0]
        ## Add the quantity and value to the item's dict
        #print("Item:", all_time_totals['items'][drop.item_id], "(before)")
        all_time_totals['items'][drop.item_id][0] += drop.quantity
        all_time_totals['items'][drop.item_id][1] += total_value
        #print("Item:", all_time_totals['items'][drop.item_id], "(after)")
        if drop.npc_id not in all_time_totals['npcs']:
            ## Add the npc to the npcs dict with an empty set of values if it doesn't exist
            all_time_totals['npcs'][drop.npc_id] = 0
        ## Add the total value to the npc's dict
        #print("NPC:", all_time_totals['npcs'][drop.npc_id], "(before)")
        all_time_totals['npcs'][drop.npc_id] += total_value
        #print("NPC:", all_time_totals['npcs'][drop.npc_id], "(after)")

        # Handle recent items (only for current partition)
        if drop_partition == current_partition:
            pass
            
            # for group_id, min_value in clan_minimums.items():
            #     if total_value >= min_value:
            #         recent_item_data = {
            #             "item_id": drop.item_id,
            #             "npc_id": drop.npc_id,
            #             "player_id": player_id,
            #             "value": total_value,
            #             "date_added": drop.date_added.isoformat()
            #         }
            #         recent_item_json = json.dumps(recent_item_data)
            #         pipeline.lpush(f"player:{player_id}:{drop_partition}:recent_items", recent_item_json)
            #         pipeline.lpush(f"player:{player_id}:all:recent_items", recent_item_json)
            #         break

    # Store totals for each partition
    for partition, totals in partition_totals.items():
        # Set total loot for partition
        pipeline.set(f"player:{player_id}:{partition}:total_loot", totals['total_loot'])
        
        # Store item totals
        for item_id, (qty, value) in totals['items'].items():
            pipeline.hset(
                f"player:{player_id}:{partition}:total_items",
                str(item_id),
                f"{qty},{value}"
            )
        
        # Store NPC totals
        for npc_id, value in totals['npcs'].items():
            pipeline.hset(
                f"player:{player_id}:{partition}:npc_totals",
                str(npc_id),
                value
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

    # Execute all Redis commands
    pipeline.execute()

    if force_update:
        print("force_update flag was set; processing complete")

def player_tester(player_id: int):
    drops = session.query(Drop).filter(Drop.player_id == player_id).all()
    print("We found a total of ", len(drops), " drops for player ", player_id)
    total_value = sum([drop.value * drop.quantity for drop in drops])
    print("The total value of these drops prior to redis processing is: ", total_value)
    update_player_in_redis(player_id, False, drops)

def player_tester_2(player_id: int, drops: List[Drop]):
    print("We found a total of ", len(drops), " drops for player ", player_id)
    total_value = sum([drop.value * drop.quantity for drop in drops])
    print("The total value of these drops prior to redis processing is: ", total_value)
    update_player_in_redis(player_id, False, drops)


if __name__ == "__main__":
    redis_client = StandaloneRedisClient()
    player_id = 1313
    partition = 202501

    # 1. Get ALL database totals
    all_drops = session.query(Drop).filter(
        Drop.player_id == player_id
    ).all()
    
    partition_drops = session.query(Drop).filter(
        Drop.player_id == player_id,
        Drop.partition == partition
    ).all()
    
    print("\nDatabase Analysis:")
    print("-" * 50)
    total_all_time = sum(drop.value * drop.quantity for drop in all_drops)
    total_partition = sum(drop.value * drop.quantity for drop in partition_drops)
    
    print(f"All-time Total: {total_all_time:,} GP")
    print(f"Partition {partition} Total: {total_partition:,} GP")
    print(f"Total Drops (all-time): {len(all_drops):,}")
    print(f"Total Drops (partition): {len(partition_drops):,}")

    # 2. Force Redis update and verify both all-time and partition values
    print("\nRedis After Update:")
    print("-" * 50)
    update_player_in_redis(player_id, True, [])
    
    redis_all = redis_client.client.get(f"player:{player_id}:all:total_loot")
    redis_partition = redis_client.client.get(f"player:{player_id}:{partition}:total_loot")
    
    redis_all_value = int(redis_all.decode('utf-8')) if redis_all else 0
    redis_partition_value = int(redis_partition.decode('utf-8')) if redis_partition else 0
    
    print(f"Redis All-time Total: {redis_all_value:,} GP")
    print(f"Redis Partition Total: {redis_partition_value:,} GP")

    # 3. Compare results
    print("\nComparison:")
    print("-" * 50)
    print(f"Database All-time:    {total_all_time:,} GP")
    print(f"Redis All-time:       {redis_all_value:,} GP")
    print(f"Difference (All):     {total_all_time - redis_all_value:,} GP")
    print(f"\nDatabase Partition:   {total_partition:,} GP")
    print(f"Redis Partition:      {redis_partition_value:,} GP")
    print(f"Difference (Part):    {total_partition - redis_partition_value:,} GP")