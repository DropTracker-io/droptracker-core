import time
from utils.redis import redis_client

def determine_key(group_id = None, 
                        partition = None,
                        npc_id = None,
                        item_id = None,
                        day = None,
                        hour = None):
    base_key = "leaderboard:"
    ## First filter to the type (group/npc/item)
    if group_id:
        base_key += f"group:{group_id}:"
    if npc_id:
        base_key += f"npc:{npc_id}:"
    if item_id:
        base_key += f"item:{item_id}:"
    if partition:
        base_key += f"{partition}:"
    else:
        base_key += f"all_time:"
    if day:
        base_key += f"{day}:"
    if hour:
        base_key += f"{hour}:"
    return base_key.rstrip(":")
        
if __name__ == "__main__":
    group_id = None
    partition = None
    npc_id = None
    item_id = None
    day = None
    hour = None
    type = "player"
    key = determine_key(npc_id=13668,partition=202505)
    print(key)
    print("Grabbing data from redis for this key...")
    time.sleep(1)
    print(redis_client.client.zrange(key, 0, -1, withscores=True))