import requests
import json
import html

sesh = requests.Session()
sesh.headers.update({'User-Agent': '@joelhalen - www.droptracker.io'})

api_url = 'https://oldschool.runescape.wiki/api.php'

def do_smwjson_query(query, jsonprops):
    global g_item_name
    # query should be a list of strings
    # jsonprops should be a list of strings, which is the list of properties that should be json.loads-ed
    
    # do request
    resp = sesh.get(api_url, params={
        'format': 'json',
        'action': 'ask',
        'query': '|'.join(query)
    })
    
    # check bad code
    if resp.status_code != 200:
        return
    
    # check for error with good code
    body = resp.json()
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
                        if drop_data.get('Dropped item') == g_item_name:  # Using the global item_name
                            filtered_vals.append(val)
                    obj[prop] = [json.loads(html.unescape(x)) for x in filtered_vals]
                else:
                    obj[prop] = [json.loads(html.unescape(x)) for x in vals]
            else:
                obj[prop] = vals
        if 'Drop JSON' not in obj or len(obj['Drop JSON']) > 0:
            out[page] = obj
    
    return out



def check_drop(item_name, npc_name) -> bool:
    global g_item_name
    g_item_name = item_name
    alt_names = {
        # Semantic name -> our database name
        "Rewards Chest (Fortis Colosseum)": "Fortis Colosseum",
        "Ancient chest": "Chambers of Xeric",
        "Monumental chest": "Theatre of Blood",  # Changed to lowercase 'chest'
        "Chest (Tombs of Amascut)": "Tombs of Amascut",
        "Reward Chest (The Gauntlet)": "The Gauntlet",
        "Chest (Barrows)": "Barrows",
        "Reward pool": "Tempoross",
        "Reward casket (easy)": "Clue Scroll (Easy)",
        "Reward casket (medium)": "Clue Scroll (Medium)",
        "Reward casket (hard)": "Clue Scroll (Hard)",
        "Reward casket (elite)": "Clue Scroll (Elite)",
        "Reward casket (master)": "Clue Scroll (Master)",
    }
    
    # Reverse the alt_names dictionary for lookup
    reverse_alt_names = {v: k for k, v in alt_names.items()}
    
    # Get the semantic name if it exists in our mapping
    semantic_name = reverse_alt_names.get(npc_name, npc_name)
    print("Checking against semantic name:", semantic_name)

    mmg_data = do_smwjson_query([
        f'[[Has subobject.Dropped item::{item_name}]]',
        '?Has subobject.Drop JSON',
        'limit=10000'
    ], ['Drop JSON'])

    print("MMG Data:", mmg_data)

    # Check each source in the data
    for source_name, source_data in mmg_data.items():
        drop_data = source_data.get('Drop JSON', [])
        for drop in drop_data:
            dropped_from = drop.get('Dropped from', '')
            # Remove any subpage references (e.g., "NPC name#Normal")
            if "#" in dropped_from:
                dropped_from = dropped_from.split("#")[0]
            
            print(f"Comparing '{dropped_from}' with '{semantic_name}'")
            # Check if this drop source matches our NPC name
            if dropped_from.lower() == semantic_name.lower():
                return True
    
    return False

def get_global_value(variable):
    resp = sesh.get(api_url, params={
        'format': 'json',
        'action': 'expandtemplates',
        'text': f'{{{{Globals|{variable}}}}}',
        'prop': 'wikitext'
    })
    
    if resp.status_code == 200:
        data = resp.json()
        return data.get('expandtemplates', {}).get('wikitext')
    return None

def get_combat_achievement_tiers():
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
            'tasks': get_global_value(f'ca {short_name} tasks'),
            'task_points': get_global_value(f'ca {short_name} task points'),
            'total_points': get_global_value(f'ca {short_name} points')
        }

    # Get total tasks
    tier_data['Total'] = {
        'tasks': get_global_value('ca total tasks'),
    }
    
    return tier_data

def get_current_ca_tier(current_points):
    current_points = int(current_points)    
    tiers = get_combat_achievement_tiers()
    
    # Define tier order from highest to lowest
    tier_order = ['Grandmaster', 'Master', 'Elite', 'Hard', 'Medium', 'Easy']
    
    print("Got tiers:", tiers)
    print("Checking current points:", current_points, "against tiers.")
    
    # Check tiers in descending order
    for tier_name in tier_order:
        if tier_name not in tiers:
            continue
        tier_data = tiers[tier_name]
        tier_points = int(tier_data['total_points'])
        print("Checking tier:", tier_name, "with points:", tier_points)
        if current_points >= tier_points:
            return tier_name
            
    return None


