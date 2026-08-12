"""
Semantic API for OSRS Wiki data using the new Bucket API.

This module handles all interactions with the OSRS Wiki's Bucket API,
replacing the deprecated Semantic MediaWiki (SMW) API.
"""

import json
import html
from datetime import date, datetime
from typing import Dict, List, Optional, Union, Any
from urllib.parse import quote


class SemanticAPI:
    """
    API client for OSRS Wiki semantic data using the Bucket API.
    """
    
    WIKI_API_URL = 'https://oldschool.runescape.wiki/api.php'

    # Row cap for the dropsline drop-source query. A query with no .limit()
    # gets the Bucket API's default of 500 rows *silently* — no truncation
    # flag, no error — and commonly-dropped items blow straight past it:
    # "Coins" has 2,069 rows across 601 sources. check_drop() then compared the
    # NPC against an arbitrary first-500 slice and read "absent" as a confident
    # negative, so e.g. a real coin drop from "Chest (Tombs of Amascut)" (row
    # ~1,700) was rejected. Ask for far more than any item legitimately has,
    # and treat a full page as inconclusive rather than as proof of a spoof.
    DROPSLINE_ROW_LIMIT = 5000

    # How long an item must have existed on the wiki before an empty
    # drop-source list is read as "the game never drops this". dropsline rows
    # come from {{DropsLine}} templates editors add to monster pages, so a
    # just-released item can have an infobox page hours before its drop table
    # is written up — precisely when its first >1M drops land. Below this age
    # "no rows" stays inconclusive.
    NEW_ITEM_GRACE_DAYS = 30

    # Mapping of database names to semantic names for compatibility
    ALT_NAMES = {
        # Semantic name -> our database name
        "Rewards Chest (Fortis Colosseum)": "Fortis Colosseum",
        "Ancient chest": ["Chambers of Xeric", "Chambers of Xeric Challenge Mode"],
        "Monumental chest": ["Theatre of Blood: Hard Mode", "Theatre of Blood"],
        "Chest (Tombs of Amascut)": ["Tombs of Amascut", "Tombs of Amascut: Expert Mode", "Tombs of Amascut: Entry Mode"],
        "Chest (Barrows)": "Barrows",
        "Reward pool": "Tempoross",
        "Reward casket (easy)": "Clue Scroll (Easy)",
        "Reward casket (medium)": "Clue Scroll (Medium)",
        "Reward casket (hard)": "Clue Scroll (Hard)",
        "Reward casket (elite)": "Clue Scroll (Elite)",
        "Reward casket (master)": "Clue Scroll (Master)",
        # npc_list's canonical spellings carry a leading "The" ("The Gauntlet",
        # "The Corrupted Gauntlet" - see ensure_npc_id_for_player's variant
        # dedup), which this alias didn't cover; every armour/weapon seed
        # drop (the only Gauntlet rewards worth >1M) was silently rejected by
        # the high-value check as a result.
        "Reward Chest (The Gauntlet)": ["The Gauntlet", "The Corrupted Gauntlet"],
        # The Royal Titans is a duo encounter; the plugin reports the source as
        # the combined "Royal Titans", but the wiki lists each titan's loot
        # under its individual name. Both map to our "Royal Titans".
        "Branda the Fire Queen": "Royal Titans",
        "Eldric the Ice King": "Royal Titans",
        # Grotesque Guardians: the plugin reports the loot-awarding boss "Dusk";
        # the wiki lists the duo's loot under the combined encounter page.
        "Grotesque Guardians": "Dusk",
        # The wiki splits Armoured zombie drops across per-location pages.
        "Armoured zombie (Zemouregal's Base)": "Armoured zombie",
        "Armoured zombie (Zemouregal's Fort)": "Armoured zombie",
        # In-raid CoX drops (e.g. Onyx) come from the boss's base wiki page,
        # but the plugin reports the enraged phase name.
        "Tekton": "Tekton (enraged)",
        # Wintertodt's reward cart page is titled just "Reward Cart".
        "Reward Cart": "Reward cart (Wintertodt)",
        # 3rd-age jewellery from elite/master caskets isn't in the dropsline
        # bucket (Treasure Trails reward tables aren't indexed); the only
        # indexed source is The Mimic, whose loot pool is exactly the
        # elite/master casket unique pool.
        "The Mimic": ["Clue Scroll (Master)", "Clue Scroll (Elite)"],
    }
    
    def __init__(self, client):
        """Initialize with reference to main client."""
        self.client = client
        self._ca_tiers_cache = None  # Cache for Combat Achievement tiers
    
    def _bucket_quote(self, value: str) -> str:
        """
        Safely quote a string for Bucket API queries using double quotes and escaping.
        """
        if value is None:
            return '""'
        # Escape backslashes and double quotes
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    
    async def _bucket_query(self, query: str) -> Dict[str, Any]:
        """
        Execute a bucket query against the OSRS Wiki API.
        
        Args:
            query: The bucket query string
            
        Returns:
            Dictionary containing the API response
        """
        session = await self.client.get_wiki_session()
        
        params = {
            'format': 'json',
            'action': 'bucket',
            'query': query,
            'formatversion': '2'
        }
        
        async with session.get(self.WIKI_API_URL, params=params) as resp:
            if resp.status != 200:
                return {}
            
            body = await resp.json()
            if 'error' in body:
                print(f"Bucket API error: {body['error']}")
                return {}
            
            return body
    
    async def get_item_id(self, item_name: str) -> Optional[int]:
        """
        Look up an item ID from the OSRS Wiki using the Bucket API.
        
        Args:
            item_name: The name of the item to look up
            
        Returns:
            The first item ID as an integer, or None if not found
        """
        try:
            # Build a safe query using properly quoted value
            query = (
                "bucket('infobox_item')"
                ".select('item_id')"
                f".where('item_name', {self._bucket_quote(item_name)}).run()"
            )
            
            result = await self._bucket_query(query)
            bucket_data = result.get('bucket', [])
            
            if bucket_data:
                # Get the first item's ID
                first_item = bucket_data[0]
                item_ids = first_item.get('item_id', [])
                if item_ids:
                    return int(item_ids[0])
            
            return None
        except Exception as e:
            print(f"Error getting item ID for {item_name}: {e}")
            return None
    
    async def get_npc_id(self, npc_name: str) -> Optional[int]:
        """
        Look up an NPC ID from the OSRS Wiki using the Bucket API.
        
        Args:
            npc_name: The name of the NPC to look up
            
        Returns:
            The first matching NPC ID as an integer, or None if not found
        """
        try:
            # Handle special cases
            if npc_name == "Corrupted Gauntlet":
                return 9035
            
            query = (
                "bucket('infobox_monster')"
                ".select('id')"
                f".where('name', {self._bucket_quote(npc_name)}).run()"
            )
            
            result = await self._bucket_query(query)
            bucket_data = result.get('bucket', [])
            
            if bucket_data:
                # Get the first NPC's ID
                first_npc = bucket_data[0]
                npc_ids = first_npc.get('id', [])
                if npc_ids:
                    return int(npc_ids[0])
            
            return None
        except Exception as e:
            print(f"Error getting NPC ID for {npc_name}: {e}")
            return None
    
    async def check_item_exists(self, item_name: str) -> bool:
        """
        Check if an item exists in the OSRS Wiki database.
        
        Args:
            item_name: The name of the item to check
            
        Returns:
            True if the item exists, False otherwise
        """
        item_id = await self.get_item_id(item_name)
        return item_id is not None
    
    @classmethod
    def _parse_wiki_release_date(cls, value: Any) -> Optional[date]:
        """Parse an infobox ``release_date`` cell ("28 August 2024") to a date.

        None when the cell is empty or in a shape we don't recognise — the
        caller treats an unknown age as "could be brand new".
        """
        if isinstance(value, list):
            value = value[0] if value else None
        if not value:
            return None
        try:
            return datetime.strptime(str(value).strip(), "%d %B %Y").date()
        except ValueError:
            return None

    async def _nothing_drops_item(self, item_name: str) -> bool:
        """Is this an item the game provably never drops?

        True only when the wiki HAS an ``infobox_item`` page under this name
        and every variant sharing the name is older than
        ``NEW_ITEM_GRACE_DAYS`` — so an empty dropsline result means "no
        monster drops this", not "nobody has documented it yet".

        False on anything short of that (name the wiki doesn't know, missing
        or unparseable release date, failed lookup), which keeps the caller on
        its fail-open path.
        """
        query = (
            "bucket('infobox_item')"
            ".select('release_date')"
            f".where('item_name', {self._bucket_quote(item_name)})"
            ".limit(50).run()"
        )
        result = await self._bucket_query(query)
        if 'bucket' not in result:
            return False

        rows = result['bucket']
        if not rows:
            # No item page under this name: our spelling, a variant we don't
            # handle, or a wiki gap. Says nothing about drop sources.
            return False

        released = [
            self._parse_wiki_release_date(row.get('release_date')) for row in rows
        ]
        if not all(released):
            # At least one variant's age is unknown, and it could be today's.
            return False

        # Judge by the NEWEST variant sharing the name: dropsline is queried by
        # name, so a just-released variant with an unwritten drop table is
        # indistinguishable from a name nothing drops.
        return (date.today() - max(released)).days > self.NEW_ITEM_GRACE_DAYS

    async def check_drop(self, item_name: str, npc_name: str) -> bool:
        """
        Check if an item drops from a specific NPC.
        
        Args:
            item_name: The name of the item
            npc_name: The name of the NPC
            
        Returns:
            True if the item drops from the NPC, False otherwise
        """
        try:
            # Handle special cases
            if item_name == "Enhanced crystal teleport seed" and npc_name == "Elf":
                return True
            if item_name.strip() == "Black tourmaline core" and npc_name.strip() == "Dusk":
                return True
            if npc_name.strip() == "Kingdom of Miscellania":
                # Kingdom resource collection is an activity, not a monster;
                # the dropsline bucket can never confirm it, so every stack
                # (coal, herbs, logs, nests) worth >1M would be a guaranteed
                # confident-negative false rejection.
                return True
            
            # Build db-name -> {wiki page_name, …}. A single submitted NPC can
            # correspond to several wiki drop-source pages: the "Royal Titans"
            # encounter's loot is split across "Branda the Fire Queen" and
            # "Eldric the Ice King"; Chambers of Xeric loot is under "Ancient
            # chest"; etc. The previous dict collapsed many-to-one, so only the
            # last alias for a given NPC ever matched.
            reverse_alt_names = {}
            for wiki_name, db_names in self.ALT_NAMES.items():
                names = db_names if isinstance(db_names, list) else [db_names]
                for db_name in names:
                    reverse_alt_names.setdefault(db_name, set()).add(wiki_name)

            # Acceptable wiki drop-source names for this NPC: the submitted name
            # itself plus any aliased wiki page names.
            acceptable = {npc_name} | reverse_alt_names.get(npc_name, set())
            acceptable_norm = {a.lower().strip() for a in acceptable}
            if acceptable_norm != {npc_name.lower().strip()}:
                print(f"Accepting drop sources {sorted(acceptable)} for {npc_name}")

            # Charged megarares (Tumeken's shadow, Scythe of vitur,
            # Sanguinesti staff, …) only ever DROP in their "(uncharged)"
            # form, and that is the only name the dropsline bucket indexes.
            # A submission carrying the charged name therefore got zero rows
            # and sailed through the not-indexed fail-open below — which is
            # how a "Tumeken's shadow from Dossier" (a container-open
            # inventory-diff artifact) was accepted on 2026-08-05. When the
            # submitted name has no rows, retry with the droppable variant:
            # its source list is authoritative for the charged form too,
            # because the charged form provably drops nowhere.
            lookup_names = [item_name]
            base_name = str(item_name or "").strip()
            if base_name and not base_name.lower().endswith("(uncharged)"):
                lookup_names.append(f"{base_name} (uncharged)")

            for lookup_name in lookup_names:
                # Query the dropsline bucket to find NPCs that drop this item.
                # The explicit .limit() is load-bearing: see DROPSLINE_ROW_LIMIT.
                query = (
                    "bucket('dropsline')"
                    ".select('page_name')"
                    f".where('item_name', {self._bucket_quote(lookup_name)})"
                    f".limit({self.DROPSLINE_ROW_LIMIT}).run()"
                )

                result = await self._bucket_query(query)

                # FAIL-OPEN on anything inconclusive. This check exists to block
                # spoofed high-value submissions, and a spoof can only be proven
                # POSITIVELY — the wiki lists real drop sources for the item and
                # this NPC isn't among them. Every other outcome (wiki
                # unavailable/rate-limited, malformed query, or the item simply
                # not indexed in `dropsline`) is *inconclusive* and must NOT be
                # treated as a spoof, or we silently reject legitimate drops.
                # `_bucket_query` returns a dict WITHOUT a 'bucket' key on transport
                # or API error (a successful query always includes 'bucket', even
                # when the list is empty), which lets us tell "the wiki couldn't
                # answer" apart from "the item has no drop sources".
                if 'bucket' not in result:
                    print(f"Dropsline lookup unavailable for {lookup_name!r}; allowing (fail-open)")
                    return True

                bucket_data = result['bucket']
                if not bucket_data:
                    # No rows under this name — try the next variant before
                    # concluding the item isn't indexed at all.
                    continue

                # Check if any of the returned NPCs match our target NPC
                for drop_entry in bucket_data:
                    dropped_from = drop_entry.get('page_name', '')

                    # Remove any subpage references (e.g., "NPC name#Normal")
                    if "#" in dropped_from:
                        dropped_from = dropped_from.split("#")[0]

                    # Check if this drop source matches our NPC name (or an alias).
                    if dropped_from.lower().strip() in acceptable_norm:
                        print(f"Drop found & valid for {item_name} from {dropped_from}")
                        return True

                # A full page means the wiki had at least as many drop-source rows
                # as we asked for, so it may have more that it never sent — "not in
                # this list" then proves nothing. Inconclusive, so fail open.
                if len(bucket_data) >= self.DROPSLINE_ROW_LIMIT:
                    print(f"Dropsline results for {lookup_name!r} hit the "
                          f"{self.DROPSLINE_ROW_LIMIT}-row query limit; allowing (fail-open)")
                    return True

                # Confident negative: the item HAS known drop sources and this NPC
                # is not one of them. This is the only case we reject.
                sources = sorted({e.get('page_name', '') for e in bucket_data})
                print(f"No valid drop found for {item_name} from {npc_name} "
                      f"(matched dropsline name {lookup_name!r}; wiki sources: {sources})")
                return False

            # No drop-source rows under any name we tried. Two very different
            # situations land here, and only one of them is inconclusive:
            #
            #  * the wiki has no item page under this name at all (our
            #    spelling, a variant we don't handle, a genuine wiki gap) —
            #    proves nothing either way;
            #  * the wiki DOES know the item and no monster's drop table lists
            #    it, i.e. nothing in the game drops it. Player-assembled and
            #    imbued gear lives here — Noxious halberd, Emberlight, Purging
            #    staff, Brimstone ring, Archers ring (i) — and for those every
            #    claimed NPC source is false by construction.
            #
            # The second case is the one this check exists to catch and used
            # to wave through. RuneLite's container-open loot records (bird
            # nests, Dossiers, Rogues' Chest — LootRecordType.EVENT) compute
            # loot by inventory diff, so an in-game inventory glitch invents
            # "loot" out of the player's own gear, which is exactly the
            # never-dropped items above. That is how a phantom 42M "Noxious
            # halberd from Rogues' Chest" was accepted on 2026-08-10, and a
            # 782M "Tumeken's shadow from Dossier" before it.
            if await self._nothing_drops_item(item_name):
                print(f"Nothing in the game drops {item_name!r} (wiki-indexed item "
                      f"with no dropsline rows); rejecting the claimed "
                      f"{npc_name} source")
                return False

            print(f"No dropsline data for {item_name!r}; allowing (fail-open)")
            return True

        except Exception as e:
            # Any unexpected error (network, parsing, …) is inconclusive — the
            # caller's high-value verification also fails open, but we return
            # True here directly so callers that treat this as a plain bool
            # (scripts, tests) don't misread an error as a confident "no".
            print(f"Error checking drop for {item_name} from {npc_name}: {e}; allowing (fail-open)")
            return True
    
    async def find_related_drops(self, item_name: str, npc_name: str) -> Dict[str, Any]:
        """
        Find all items that drop from a specific NPC.
        
        Args:
            item_name: Target item name (for context)
            npc_name: The NPC to find drops for
            
        Returns:
            Dictionary containing all drops from the NPC
        """
        try:
            # Create reverse mapping for alternative names
            reverse_alt_names = {}
            for semantic_name, db_names in self.ALT_NAMES.items():
                if isinstance(db_names, list):
                    for db_name in db_names:
                        reverse_alt_names[db_name] = semantic_name
                else:
                    reverse_alt_names[db_names] = semantic_name
            
            # Get the semantic name if it exists in our mapping
            semantic_name = reverse_alt_names.get(npc_name, npc_name)
            
            # Query all drops from this NPC
            query = (
                "bucket('dropsline')"
                ".select('item_name', 'page_name')"
                f".where('page_name', {self._bucket_quote(semantic_name)}).run()"
            )
            
            result = await self._bucket_query(query)
            bucket_data = result.get('bucket', [])
            
            all_drops = []
            for drop_entry in bucket_data:
                dropped_item = drop_entry.get('item_name', '')
                dropped_from = drop_entry.get('page_name', '')
                
                # Remove any subpage references
                if "#" in dropped_from:
                    dropped_from = dropped_from.split("#")[0]
                
                if dropped_from.lower() == semantic_name.lower():
                    all_drops.append({
                        "item_name": dropped_item,
                        "rarity": "Unknown",  # Rarity not available in dropsline bucket
                        "npc_name": dropped_from
                    })
            
            return {
                "target_item": item_name,
                "npc_name": semantic_name,
                "all_drops": all_drops
            }
            
        except Exception as e:
            print(f"Error finding related drops for {npc_name}: {e}")
            return {
                "target_item": item_name,
                "npc_name": npc_name,
                "all_drops": []
            }
    
    async def get_global_value(self, variable: str) -> Optional[str]:
        """
        Get a global variable value from the OSRS Wiki.
        
        Args:
            variable: The global variable name
            
        Returns:
            The variable value as a string, or None if not found
        """
        try:
            session = await self.client.get_wiki_session()
            
            params = {
                'format': 'json',
                'action': 'expandtemplates',
                'text': f'{{{{Globals|{variable}}}}}',
                'prop': 'wikitext'
            }
            
            async with session.get(self.WIKI_API_URL, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('expandtemplates', {}).get('wikitext')
            
            return None
        except Exception as e:
            print(f"Error getting global value for {variable}: {e}")
            return None
    
    async def get_combat_achievement_tiers(self) -> Dict[str, Dict[str, str]]:
        """
        Get combat achievement tier information with caching.
        
        Returns:
            Dictionary containing tier data with tasks and points
        """
        # Return cached data if available
        if self._ca_tiers_cache is not None:
            return self._ca_tiers_cache
        
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
                'tasks': await self.get_global_value(f'ca {short_name} tasks'),
                'task_points': await self.get_global_value(f'ca {short_name} task points'),
                'total_points': await self.get_global_value(f'ca {short_name} points')
            }
        
        # Get total tasks
        tier_data['Total'] = {
            'tasks': await self.get_global_value('ca total tasks'),
        }
        
        # Cache the result
        self._ca_tiers_cache = tier_data
        return tier_data
    
    async def get_ca_tier_progress(self, current_points: int) -> tuple[float, int]:
        """
        Calculate combat achievement tier progress.
        
        Args:
            current_points: Current CA points
            
        Returns:
            Tuple of (progress_percentage, next_tier_points)
        """
        current_points = int(current_points)
        tiers = await self.get_combat_achievement_tiers()
        
        # Define tier order from lowest to highest
        tier_order = ['Easy', 'Medium', 'Hard', 'Elite', 'Master', 'Grandmaster']
        
        # Find which tier the player is currently in and what's next
        current_tier = None
        next_tier = None
        current_tier_points = 0
        next_tier_points = 0
        
        # First find the current tier
        for i, tier_name in enumerate(tier_order):
            if tier_name not in tiers or not tiers[tier_name]['total_points']:
                continue
            
            tier_points = int(tiers[tier_name]['total_points'])
            
            if current_points >= tier_points:
                current_tier = tier_name
                current_tier_points = tier_points
                # Look ahead to next tier
                if i + 1 < len(tier_order) and tier_order[i + 1] in tiers:
                    next_tier = tier_order[i + 1]
                    if tiers[next_tier]['total_points']:
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
            if 'Easy' in tiers and tiers['Easy']['total_points']:
                easy_points = int(tiers['Easy']['total_points'])
                progress = (current_points / easy_points) * 100
                return round(progress, 2), easy_points
            return 0.0, 0
        elif next_tier is None:
            # Completed Grandmaster
            if 'Grandmaster' in tiers and tiers['Grandmaster']['total_points']:
                return 100.0, int(tiers['Grandmaster']['total_points'])
            return 100.0, current_tier_points
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
                print(f"Error calculating CA progress: {e}")
                return 0.0, next_tier_points
    
    async def get_current_ca_tier(self, current_points: int) -> Optional[str]:
        """
        Get the current combat achievement tier for given points.
        
        Args:
            current_points: Current CA points
            
        Returns:
            Current tier name or None
        """
        current_points = int(current_points)
        tiers = await self.get_combat_achievement_tiers()
        
        # Define tier order from highest to lowest
        tier_order = ['Grandmaster', 'Master', 'Elite', 'Hard', 'Medium', 'Easy']
        
        # Check tiers in descending order
        for tier_name in tier_order:
            if tier_name not in tiers or not tiers[tier_name]['total_points']:
                continue
            
            tier_points = int(tiers[tier_name]['total_points'])
            if current_points >= tier_points:
                return tier_name
        
        return None
    
    async def get_ca_info(self, current_points: int) -> Dict[str, Any]:
        """
        Get complete Combat Achievement information for given points in a single call.
        
        This is more efficient than calling get_current_ca_tier() and get_ca_tier_progress()
        separately as it only fetches the tier data once.
        
        Args:
            current_points: Current CA points
            
        Returns:
            Dictionary containing current tier, progress, and next tier info
        """
        current_points = int(current_points)
        tiers = await self.get_combat_achievement_tiers()
        
        # Define tier order from lowest to highest
        tier_order = ['Easy', 'Medium', 'Hard', 'Elite', 'Master', 'Grandmaster']
        
        # Find current and next tier
        current_tier = None
        next_tier = None
        current_tier_points = 0
        next_tier_points = 0
        
        # Find the current tier
        for i, tier_name in enumerate(tier_order):
            if tier_name not in tiers or not tiers[tier_name]['total_points']:
                continue
            
            tier_points = int(tiers[tier_name]['total_points'])
            
            if current_points >= tier_points:
                current_tier = tier_name
                current_tier_points = tier_points
                # Look ahead to next tier
                if i + 1 < len(tier_order) and tier_order[i + 1] in tiers:
                    next_tier = tier_order[i + 1]
                    if tiers[next_tier]['total_points']:
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
            if 'Easy' in tiers and tiers['Easy']['total_points']:
                easy_points = int(tiers['Easy']['total_points'])
                progress = (current_points / easy_points) * 100
                return {
                    'current_tier': None,
                    'next_tier': 'Easy',
                    'progress_percentage': round(progress, 2),
                    'current_points': current_points,
                    'current_tier_points': 0,
                    'next_tier_points': easy_points
                }
        elif next_tier is None:
            # Completed Grandmaster
            return {
                'current_tier': 'Grandmaster',
                'next_tier': None,
                'progress_percentage': 100.0,
                'current_points': current_points,
                'current_tier_points': current_tier_points,
                'next_tier_points': current_tier_points
            }
        else:
            # Calculate progress to next tier
            points_needed = next_tier_points - current_tier_points
            if points_needed == 0:
                progress = 100.0
            else:
                points_gained = current_points - current_tier_points
                progress = (points_gained / points_needed) * 100
            
            return {
                'current_tier': current_tier,
                'next_tier': next_tier,
                'progress_percentage': round(progress, 2),
                'current_points': current_points,
                'current_tier_points': current_tier_points,
                'next_tier_points': next_tier_points
            }
        
        # Fallback
        return {
            'current_tier': None,
            'next_tier': None,
            'progress_percentage': 0.0,
            'current_points': current_points,
            'current_tier_points': 0,
            'next_tier_points': 0
        }
