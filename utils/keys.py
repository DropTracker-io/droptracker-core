import time

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
        