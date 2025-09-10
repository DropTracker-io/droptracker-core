from db.models import Base, session
from sqlalchemy import Column, Integer, String, DateTime, Boolean, TINYINT, text, func, JSON
from sqlalchemy.orm import relationship
import json


""" Example loot_data:
    "item_id": [quantity, TOTAL value]
    {
        "20997": [1, 2142572522],
        "4151": [1, 300000],
    }
"""

def add_data_to_player_loot(loot_data, item_id, quantity, total_value):
    """
        Encodes the loot data created by the loop to be stored in the database properly
    """
    if item_id not in loot_data:
        loot_data[item_id] = [quantity, total_value]
    else:
        loot_data[item_id][0] += quantity
        loot_data[item_id][1] += total_value
    return json.dumps(loot_data)

def decode_player_loot_data(encoded_data):
    """
        Decodes the loot data from a JSON object
    """
    return json.loads(encoded_data)
