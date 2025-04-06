import requests
from datetime import datetime
import sys

# Base URLs for the APIs
PRICES_API_BASE = "https://prices.runescape.wiki/api/v1/osrs"
WIKI_API_BASE = "https://oldschool.runescape.wiki/api.php"

def get_true_item_value(item_name, provided_value: int = 0):
    # Check if an incoming item matches our defined list of
    # untradeables or otherwise unvalued items that hold a value indirectly
    # for example, an ultor vestige has a 5M untradeable drop value, but actually is worth
    # An ultor ring, minus 3 Chromium Ingots
    item_lower = item_name.lower()
    if "vestige" in item_lower:
        ring = item_lower.replace("vestige", "ring")
        ring_price = get_most_recent_price_by_name(ring)
        ingot_price = get_most_recent_price_by_name("Chromium ingot")
        return ring_price - (ingot_price * 3)
    if "bludgeon" in item_lower:
        if item_lower == "bludgeon axon" or item_lower == "bludgeon claw" or item_lower == "bludgeon spine":
            bludgeon_value = get_most_recent_price_by_name("Abyssal bludgeon")
            return int(bludgeon_value / 3)
        else:
            return provided_value
    if item_lower == "hydra's eye" or item_lower == "hydra's fang" or item_lower == "hydra's heart":
        brimstone_value = get_most_recent_price_by_name("Brimstone ring")
        return int(brimstone_value / 3)
    if "noxious" in item_lower:
        noxious_halberd_value = get_most_recent_price_by_name("Noxious halberd")
        if "point" in item_lower or "blade" in item_lower or "pommel" in item_lower:
            return int(noxious_halberd_value / 3)
        else:
            return provided_value
    if item_lower == "araxyte fang":
        amulet_of_rancour_value = get_most_recent_price_by_name("Amulet of rancour")
        torture_value = get_most_recent_price_by_name("Amulet of torture")
        return amulet_of_rancour_value - torture_value
    else:
        return provided_value

# Create a dedicated session for the prices API with a proper User-Agent
prices_session = requests.Session()
prices_session.headers.update({
    'User-Agent': 'DropTracker.io - GE Price API Integration - @joelhalen'
})

# Create a session for wiki API requests
wiki_session = requests.Session()
wiki_session.headers.update({
    'User-Agent': 'DropTracker.io - GE Price API Integration - @joelhalen'
})

def get_mapping():
    """Fetch the item mapping data which contains names, IDs, and other metadata"""
    endpoint = f"{PRICES_API_BASE}/mapping"
    resp = prices_session.get(endpoint)
    
    if resp.status_code != 200:
        return None
    
    return resp.json()

def find_item_id_by_name(name):
    """Find an item ID by name using the mapping data"""
    mapping_data = get_mapping()
    if not mapping_data:
        return None
    
    name_lower = name.lower()
    for item in mapping_data:
        if item.get('name', '').lower() == name_lower:
            return item['id']
    return None

def get_latest_price_data(item_id):
    """Fetch the latest price data from the real-time prices API"""
    endpoint = f"{PRICES_API_BASE}/latest"
    params = {'id': item_id}
    
    resp = prices_session.get(endpoint, params=params)
    
    if resp.status_code != 200:
        return None
    
    data = resp.json()
    
    if 'data' not in data:
        return None
    
    item_data = data['data'].get(str(item_id))
    if not item_data:
        return None
    
    return item_data

def get_most_recent_price_by_id(item_id):
    """
    Get the most recent price for an item by ID
    Returns the price as an integer, or None if not found
    """
    if not item_id:
        return None
    
    price_data = get_latest_price_data(item_id)
    if not price_data:
        return None
    
    high_price = price_data.get('high')
    low_price = price_data.get('low')
    high_time = price_data.get('highTime')
    low_time = price_data.get('lowTime')
    
    # Determine the most recent price
    if high_price and low_price and high_time and low_time:
        if high_time > low_time:
            return high_price
        else:
            return low_price
    elif high_price and high_time:
        return high_price
    elif low_price and low_time:
        return low_price
    
    return None

def get_most_recent_price_by_name(item_name):
    """
    Get the most recent price for an item by name
    Returns the price as an integer, or None if not found
    """
    item_id = find_item_id_by_name(item_name)
    if not item_id:
        return None
    
    return get_most_recent_price_by_id(item_id)

