import aiohttp
import json
import html
from utils.logger import LoggerClient

logger = LoggerClient()

api_url = 'https://oldschool.runescape.wiki/api.php'

# Create a single aiohttp session for reuse
aiohttp_session = None

alt_names = {
        # Semantic name -> our database name
        "Rewards Chest (Fortis Colosseum)": "Fortis Colosseum",
        "Ancient chest": ["Chambers of Xeric", "Chambers of Xeric Challenge Mode"],
        "Monumental chest": ["Theatre of Blood: Hard Mode", "Theatre of Blood"],
        "Chest (Tombs of Amascut)": ["Tombs of Amascut", "Tombs of Amascut: Expert Mode"],
        "Chest (Barrows)": "Barrows",
        "Reward pool": "Tempoross",
        "Reward casket (easy)": "Clue Scroll (Easy)",
        "Reward casket (medium)": "Clue Scroll (Medium)",
        "Reward casket (hard)": "Clue Scroll (Hard)",
        "Reward casket (elite)": "Clue Scroll (Elite)",
        "Reward casket (master)": "Clue Scroll (Master)",
        "Reward Chest (The Gauntlet)": "Corrupted Gauntlet" ## Semantic has no drops stored for 'Corrupted Gauntlet'.
    }


async def get_aiohttp_session():
    global aiohttp_session
    if aiohttp_session is None or aiohttp_session.closed:
        aiohttp_session = aiohttp.ClientSession(headers={'User-Agent': '@joelhalen - www.droptracker.io'})
    return aiohttp_session

async def do_smwjson_query(query, jsonprops, use_global=True):
    if use_global:
        global g_item_name
    # query should be a list of strings
    # jsonprops should be a list of strings, which is the list of properties that should be json.loads-ed
    
    session = await get_aiohttp_session()
    async with session.get(api_url, params={
        'format': 'json',
        'action': 'ask',
        'query': '|'.join(query)
    }) as resp:
        if resp.status != 200:
            return
        body = await resp.json()
        if 'error' in body:
            return

        data = body.get('query', {}).get('results')
        # check we have results
        if data is None or len(data) == 0:
            return {}
        
        # example output format
        out = {}
        for page, smwresp in data.items():
            obj = {
                'page': page,
                'url': smwresp['fullurl']
            }
            for prop, vals in smwresp['printouts'].items():
                if prop in jsonprops:
                    # Filter Drop JSON to only include entries for our specific item
                    if prop == 'Drop JSON':
                        filtered_vals = []
                        for val in vals:
                            drop_data = json.loads(html.unescape(val))
                            if use_global and drop_data.get('Dropped item') == g_item_name:  # Using the global item_name
                                filtered_vals.append(val)
                            else:
                                filtered_vals.append(val)
                        obj[prop] = [json.loads(html.unescape(x)) for x in filtered_vals]
                    else:
                        obj[prop] = [json.loads(html.unescape(x)) for x in vals]
                else:
                    obj[prop] = vals
            if 'Drop JSON' not in obj or len(obj['Drop JSON']) > 0:
                out[page] = obj
        
        return out


async def do_raw_smwjson_query(query, jsonprops):
    # query should be a list of strings
    # jsonprops should be a list of strings, which is the list of properties that should be json.loads-ed
    
    session = await get_aiohttp_session()
    async with session.get(api_url, params={
        'format': 'json',
        'action': 'ask',
        'query': '|'.join(query)
    }) as resp:
        if resp.status != 200:
            return
        body = await resp.json()
        if 'error' in body:
            return

        data = body.get('query', {}).get('results')
        # check we have results
        if data is None or len(data) == 0:
            return {}
        
        out = {}
        for page, smwresp in data.items():
            obj = {
                'page': page,
                'url': smwresp['fullurl']
            }
            for prop, vals in smwresp['printouts'].items():
                if prop in jsonprops:
                    obj[prop] = [json.loads(html.unescape(x)) for x in vals]
                else:
                    obj[prop] = vals
            out[page] = obj
        
        return out

async def get_npc_id(npc_name: str) -> int:
    if not npc_name:
        return None
    """
    Look up an NPC ID from the OSRS Wiki using the semantic API.
    
    Args:
        npc_name: The name of the NPC to look up
        
    Returns:
        The first matching NPC ID as an integer, or None if not found
    """
    try:
        # Handle special cases
        if npc_name == "Corrupted Gauntlet":
            return 9035
        
        # Get the raw page content from NPC IDs page
        session = await get_aiohttp_session()
        async with session.get('https://oldschool.runescape.wiki/api.php', params={
            'action': 'parse',
            'page': 'NPC_IDs',
            'format': 'json',
            'prop': 'text'
        }) as resp:
            if resp.status != 200:
                logger.log_sync(log_type="error", message=f"Failed to fetch NPC IDs page: {resp.status}", context="semantic_check.py")
                return None
                
            data = await resp.json()
            if 'error' in data:
                logger.log_sync(log_type="error", message=f"Wiki API error: {data['error']}", context="semantic_check.py")
                return None
                
            # Extract the HTML content
            html_content = data.get('parse', {}).get('text', {}).get('*', '')
            
            logger.log_sync(log_type="debug", message=f"Searching for NPC: {npc_name} in NPC IDs page", context="semantic_check.py")
            
            # Look for table rows containing the NPC name
            import re
            
            # This looks for links containing the NPC name followed by the ID in the next cell
            pattern = r'<td>(?:<span class="smw-subobject-entity">)?<a href="[^"]+" title="([^"]+)">[^<]+</a>(?:</span>)?\s*</td>\s*<td><a[^>]+>(\d+)</a>'
            matches = re.findall(pattern, html_content)
            
            for name, npc_id in matches:
                # Extract the base name without any variant info (remove text after #)
                base_name = name.split('#')[0]
                
                # Case-insensitive comparison
                if base_name.lower().strip() == npc_name.lower().strip():
                    npc_id = int(npc_id)
                    logger.log_sync(log_type="info", message=f"Found NPC ID {npc_id} for {npc_name}", context="semantic_check.py")
                    return npc_id
            
            logger.log_sync(log_type="info", message=f"No NPC ID found for {npc_name} in NPC IDs page", context="semantic_check.py")
            return None
            
    except Exception as e:
        logger.log_sync(log_type="error", message=f"Error getting NPC ID for {npc_name}: {e}", context="semantic_check.py")
        return None
    finally:
        await close_aiohttp_session()

async def check_item_exists(item_name: str) -> bool:
    item_id = await get_item_id(item_name)
    return item_id is not None


async def get_item_id(item_name: str) -> int:
    """
    Look up an item ID from the OSRS Wiki using the semantic API.
    
    
    Args:
        item_name: The name of the item to look up
        
    Returns:
        The item ID as an integer, or None if not found
        This uses the core (first) item ID provided, not exactly the only item id for this item..
    """
    try:
        # Query the semantic API for the item
        item_data = await do_raw_smwjson_query([
            f'[[{item_name}]]',
            '?Item ID',
            'limit=1'
        ], [])
        
        if not item_data:
            return None
        
        for page, data in item_data.items():
            item_ids = data.get('Item ID', [])
            if item_ids:
                # Return the first item ID
                item_id = int(item_ids[0])
                return item_id
        
        return None
    except Exception as e:
        return None
    finally:
        await close_aiohttp_session()

async def find_related_drops(item_name: str, npc_name: str) -> dict:
    reverse_alt_names = {}
    for semantic_name, db_names in alt_names.items():
        if isinstance(db_names, list):
            for db_name in db_names:
                reverse_alt_names[db_name] = semantic_name
        else:
            reverse_alt_names[db_names] = semantic_name
    
    # Get the semantic name if it exists in our mapping
    semantic_name = reverse_alt_names.get(npc_name, npc_name)
    npc_name = semantic_name

    smw_data = await do_smwjson_query([
        f'[[Has subobject.Dropped from::{npc_name}]]',
        '?Has subobject.Drop JSON',
        'limit=10000'
    ], ['Drop JSON'], use_global=False)
    
    all_drops = []
    checked_npcs = set()
    
    for source_name, source_data in smw_data.items():
        drop_data = source_data.get('Drop JSON', [])
        for drop in drop_data:
            dropped_item = drop.get('Dropped item', '')
            dropped_from = drop.get('Dropped from', '')
            rarity = drop.get('Rarity', '')
            
            if "#" in dropped_from:
                dropped_from = dropped_from.split("#")[0]
            
            if dropped_from.lower() == npc_name.lower():
                all_drops.append({
                    "item_name": dropped_item,
                    "rarity": rarity,
                    "npc_name": dropped_from
                })
    
    return {
        "target_item": item_name,
        "npc_name": npc_name,
        "all_drops": all_drops
    }

async def check_drop(item_name: str, npc_name: str) -> bool:
    if item_name == "Enhanced crystal teleport seed" and npc_name == "Elf":
        return True
    global g_item_name
    g_item_name = item_name
    if item_name.strip() == "Black tourmaline core":
        if npc_name.strip() == "Dusk":
            return True
    
    # Create reverse mapping that handles lists of values
    reverse_alt_names = {}
    for semantic_name, db_names in alt_names.items():
        if isinstance(db_names, list):
            for db_name in db_names:
                reverse_alt_names[db_name] = semantic_name
        else:
            reverse_alt_names[db_names] = semantic_name
    
    # Get the semantic name if it exists in our mapping
    semantic_name = reverse_alt_names.get(npc_name, npc_name)
    if semantic_name != npc_name:
        logger.log_sync(log_type="access", message=f"Using semantic name: {semantic_name} for {npc_name}", context="semantic_check.py")

    mmg_data = await do_smwjson_query([
        f'[[Has subobject.Dropped item::{item_name}]]',
        '?Has subobject.Drop JSON',
        'limit=10000'
    ], ['Drop JSON'], use_global=True)

    #print("MMG Data:", mmg_data)

    # Check each source in the data
    for source_name, source_data in mmg_data.items():
        drop_data = source_data.get('Drop JSON', [])
        for drop in drop_data:
            dropped_from = drop.get('Dropped from', '')
            # Remove any subpage references (e.g., "NPC name#Normal")
            if "#" in dropped_from:
                dropped_from = dropped_from.split("#")[0]
            
            #print(f"Comparing '{dropped_from}' with '{semantic_name}'")
            # Check if this drop source matches our NPC name
            if dropped_from.lower() == semantic_name.lower():
                logger.log_sync(log_type="access", message=f"Drop found & valid for {item_name} from {dropped_from}", context="semantic_check.py")
                return True
    
    logger.log_sync(log_type="access", message=f"No valid drop found for {item_name} from {semantic_name}", context="semantic_check.py")
    return False

async def get_global_value(variable):
    session = await get_aiohttp_session()
    async with session.get(api_url, params={
        'format': 'json',
        'action': 'expandtemplates',
        'text': f'{{{{Globals|{variable}}}}}',
        'prop': 'wikitext'
    }) as resp:
        if resp.status == 200:
            data = await resp.json()
            return data.get('expandtemplates', {}).get('wikitext')
    return None

async def get_combat_achievement_tiers():
    # Map short names to full names
    tier_mapping = {
        'easy': 'Easy',
        'medium': 'Medium',
        'hard': 'Hard',
        'elite': 'Elite',
        'master': 'Master',
        'gm': 'Grandmaster'
    }
    
    tier_data = {}

    # Use full names when setting the values
    for short_name, full_name in tier_mapping.items():
        tier_data[full_name] = {
            'tasks': await get_global_value(f'ca {short_name} tasks'),
            'task_points': await get_global_value(f'ca {short_name} task points'),
            'total_points': await get_global_value(f'ca {short_name} points')
        }

    # Get total tasks
    tier_data['Total'] = {
        'tasks': await get_global_value('ca total tasks'),
    }
    
    return tier_data

async def get_ca_tier_progress(current_points):
    current_points = int(current_points)    
    tiers = await get_combat_achievement_tiers()
    
    # Define tier order from lowest to highest
    tier_order = ['Easy', 'Medium', 'Hard', 'Elite', 'Master', 'Grandmaster']
    
    # Find which tier the player is currently in and what's next
    current_tier = None
    next_tier = None
    
    # First find the current tier
    for i, tier_name in enumerate(tier_order):
        if tier_name not in tiers:
            continue
            
        tier_points = int(tiers[tier_name]['total_points'])
        
        if current_points >= tier_points:
            current_tier = tier_name
            current_tier_points = tier_points
            # Look ahead to next tier
            if i + 1 < len(tier_order) and tier_order[i + 1] in tiers:
                next_tier = tier_order[i + 1]
                next_tier_points = int(tiers[next_tier]['total_points'])
        else:
            # If we haven't reached this tier, it's our next goal
            if current_tier is None:
                next_tier = tier_name
                next_tier_points = tier_points
                current_tier_points = 0
            break
    
    # Calculate progress
    if current_tier is None:
        # Haven't reached Easy tier yet
        easy_points = int(tiers['Easy']['total_points'])
        progress = (current_points / easy_points) * 100
        return round(progress, 2), easy_points
    elif next_tier is None:
        # Completed Grandmaster
        return 100.0, int(tiers['Grandmaster']['total_points'])
    else:
        # Calculate progress to next tier
        points_needed = next_tier_points - current_tier_points
        if points_needed == 0:
            return 100.0, next_tier_points
        points_gained = current_points - current_tier_points
        try:
            progress = (points_gained / points_needed) * 100
            return round(progress, 2), next_tier_points
        except Exception as e:
            print("Error calculating CA progress:", e)
            return 0.0, next_tier_points

async def get_current_ca_tier(current_points):
    current_points = int(current_points)    
    tiers = await get_combat_achievement_tiers()
    
    # Define tier order from highest to lowest
    tier_order = ['Grandmaster', 'Master', 'Elite', 'Hard', 'Medium', 'Easy', 'None']
    
    #print("Got tiers:", tiers)
    #print("Checking current points:", current_points, "against tiers.")
    
    # Check tiers in descending order
    for tier_name in tier_order:
        if tier_name not in tiers:
            continue
        tier_data = tiers[tier_name]
        if tier_data is None:
            continue
        tier_points = int(tier_data['total_points'])
        #print("Checking tier:", tier_name, "with points:", tier_points)
        if current_points >= tier_points:
            return tier_name
            
    return None

# Optionally, add a cleanup function to close the aiohttp session on shutdown
async def close_aiohttp_session():
    global aiohttp_session
    if aiohttp_session is not None and not aiohttp_session.closed:
        await aiohttp_session.close()


