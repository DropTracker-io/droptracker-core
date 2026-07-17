import aiohttp
import asyncio
from datetime import datetime
import sys

from utils.redis import RedisClient
from utils import value_overrides

# Base URLs for the APIs
PRICES_API_BASE = "https://prices.runescape.wiki/api/v1/osrs"
WIKI_API_BASE = "https://oldschool.runescape.wiki/api.php"

_redis_client = RedisClient()
ITEM_PRICE_CACHE_TTL = 3600  # seconds (1 hour)

# Create a single aiohttp session for reuse
prices_session = None
wiki_session = None

async def get_prices_session():
    global prices_session
    if prices_session is None or prices_session.closed:
        prices_session = aiohttp.ClientSession(headers={
            'User-Agent': 'DropTracker.io - GE Price API Integration - @joelhalen'
        })
    return prices_session

async def get_wiki_session():
    global wiki_session
    if wiki_session is None or wiki_session.closed:
        wiki_session = aiohttp.ClientSession(headers={
            'User-Agent': 'DropTracker.io - GE Price API Integration - @joelhalen'
        })
    return wiki_session

async def _lookup_and_cache_ge_price(item_name: str, item_id=None) -> int:
    """Fetch GE price for an item and cache it in Redis. Returns 0 on failure."""
    cache_key = f"item_price:{item_id}" if item_id else f"item_price_name:{item_name.lower()}"
    cached = _redis_client.get(cache_key)
    if cached is not None:
        try:
            return int(cached)
        except (ValueError, TypeError):
            pass

    price = None
    if item_id:
        price = await get_most_recent_price_by_id(item_id)
    if not price:
        price = await get_most_recent_price_by_name(item_name)

    if price:
        _redis_client.setex(cache_key, ITEM_PRICE_CACHE_TTL, str(price))
        return int(price)
    return 0


async def build_component_price_map(overrides) -> dict:
    """Fetch each distinct component price once, in parallel, for a batch of
    overrides. Used by the admin preview and the public /item-values listing so
    they don't hit the GE API once per (override × component). The result is
    keyed by ``value_overrides.component_price_key`` to match
    ``compute_override_from_prices``."""
    wanted: dict = {}
    for override in overrides:
        for component in override.get("components") or []:
            wanted.setdefault(value_overrides.component_price_key(component), component)

    async def _fetch(component):
        price = None
        component_id = component.get("item_id")
        if component_id:
            price = await get_most_recent_price_by_id(component_id)
        if not price and component.get("item_name"):
            price = await get_most_recent_price_by_name(component["item_name"])
        return price

    keys = list(wanted)
    prices = await asyncio.gather(*(_fetch(wanted[k]) for k in keys))
    return dict(zip(keys, prices))


async def _compute_override_value(override: dict):
    """Value a single dropped item from its override rule (drop-path entry)."""
    price_map = await build_component_price_map([override])
    return value_overrides.compute_override_from_prices(override, price_map)


async def get_true_item_value(item_name, provided_value: int = 0, item_id=None):
    # Some items are dropped with a 0gp value but are worth something because
    # they're a component of a tradeable item (e.g. an ultor vestige is worth an
    # ultor ring minus 3 Chromium ingots; a bludgeon axon is worth 1/3 of an
    # Abyssal bludgeon). These rules live in the item_value_overrides table and
    # are editable at runtime from the admin dashboard — see utils/value_overrides.py.
    override = value_overrides.match(item_id, item_name)
    if override:
        computed = await _compute_override_value(override)
        if computed is not None:
            return computed
        # A component couldn't be priced: use the rule's flat fallback, or the
        # value the client reported when no fallback is configured.
        fallback = override.get("fallback_value") or 0
        return fallback if fallback else provided_value

    # The client-supplied value is spoofable, so prefer the server GE price for
    # any priceable item; fall back to the client value only when unpriceable.
    server_price = await _lookup_and_cache_ge_price(item_name, item_id)
    if server_price:
        return server_price
    return provided_value

async def get_mapping():
    """Fetch the item mapping data which contains names, IDs, and other metadata"""
    endpoint = f"{PRICES_API_BASE}/mapping"
    session = await get_prices_session()
    async with session.get(endpoint) as resp:
        if resp.status != 200:
            return None
        return await resp.json()

async def find_item_id_by_name(name):
    """Find an item ID by name using the mapping data"""
    mapping_data = await get_mapping()
    if not mapping_data:
        return None
    
    name_lower = name.lower()
    for item in mapping_data:
        if item.get('name', '').lower() == name_lower:
            return item['id']
    return None

async def get_latest_price_data(item_id):
    """Fetch the latest price data from the real-time prices API"""
    endpoint = f"{PRICES_API_BASE}/latest"
    params = {'id': item_id}
    session = await get_prices_session()
    async with session.get(endpoint, params=params) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        
        if 'data' not in data:
            return None
        
        item_data = data['data'].get(str(item_id))
        if not item_data:
            return None
        
        return item_data

async def get_most_recent_price_by_id(item_id):
    """
    Get the most recent price for an item by ID
    Returns the price as an integer, or None if not found
    """
    if not item_id:
        return None
    
    price_data = await get_latest_price_data(item_id)
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

async def get_most_recent_price_by_name(item_name):
    """
    Get the most recent price for an item by name
    Returns the price as an integer, or None if not found
    """
    item_id = await find_item_id_by_name(item_name)
    if not item_id:
        return None
    
    return await get_most_recent_price_by_id(item_id)

async def close_aiohttp_sessions():
    global prices_session, wiki_session
    if prices_session is not None and not prices_session.closed:
        await prices_session.close()
    if wiki_session is not None and not wiki_session.closed:
        await wiki_session.close()

