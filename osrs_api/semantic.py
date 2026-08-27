"""
Semantic API for OSRS Wiki data using the new Bucket API.

This module handles all interactions with the OSRS Wiki's Bucket API,
replacing the deprecated Semantic MediaWiki (SMW) API.
"""

import asyncio
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

    # Drop tables only change with the weekly game update (Tue/Wed), so a
    # per-item source list is safe to reuse for days. Serving allows from
    # cache is what keeps our api.php volume down — the wiki blocklisted our
    # previous User-Agent for hammering it once per >1M drop. Staleness can
    # only ever delay an ALLOW for a freshly-added source: any REJECT that was
    # derived from cached data is re-checked against the live wiki before it
    # is issued (see check_drop), so the TTL never causes a false rejection.
    DROP_CACHE_TTL_SECONDS = 7 * 24 * 3600

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
        # Optional redis-like cache (get/set(ex=)) supplied by the client;
        # None disables drop-source caching entirely.
        self.cache = getattr(client, "cache", None)
        self._ca_tiers_cache = None  # Cache for Combat Achievement tiers

    def _cache_get(self, key: str):
        """JSON-decode a cached value; None on miss, no cache, or any error.

        The cache is an availability optimization, never a correctness
        dependency — a broken/unreachable cache must degrade to the uncached
        wiki path, not take drop verification down with it.
        """
        if self.cache is None:
            return None
        try:
            raw = self.cache.get(key)
        except Exception as e:
            print(f"Drop-check cache read failed for {key}: {e}")
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def _cache_set(self, key: str, value) -> None:
        if self.cache is None:
            return
        try:
            self.cache.set(key, json.dumps(value), ex=self.DROP_CACHE_TTL_SECONDS)
        except Exception as e:
            print(f"Drop-check cache write failed for {key}: {e}")


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
                # Log it: a silent non-200 is how a UA blocklisting in
                # 2026-08 turned every high-value check into a fail-open for
                # days with nothing in the journal but "unavailable".
                print(f"Bucket API HTTP {resp.status} for query: {query[:160]}")
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

    # Combat achievement tasks are enumerable, unlike drop sources, so this
    # fetches the whole table once rather than querying per name. There are
    # ~650 of them and the list only moves on a game update; the caller
    # (utils/ca_tasks.py) caches it for a week.
    CA_TASK_PAGE_SIZE = 500

    async def get_combat_achievement_names(self) -> List[str]:
        """Every combat achievement task name the wiki knows.

        Empty list on any failure — the caller must read that as "could not
        confirm", never as "no such task", because an empty answer here would
        otherwise invalidate every task in the game.

        Paginates explicitly: an unlimited Bucket query silently returns the
        API's default 500 rows with no truncation flag (the same trap
        documented on DROPSLINE_ROW_LIMIT), and the task list is already past
        that.
        """
        names: List[str] = []
        seen = set()
        offset = 0
        try:
            while True:
                query = (
                    "bucket('combat_achievement')"
                    ".select('name')"
                    f".limit({self.CA_TASK_PAGE_SIZE}).offset({offset}).run()"
                )
                result = await self._bucket_query(query)
                if 'bucket' not in result:
                    # Transport or API error — distinguishable from an empty
                    # page, and it must not look like the end of the table.
                    print(f"CA task fetch failed at offset {offset}")
                    return []
                rows = result.get('bucket') or []
                for row in rows:
                    value = row.get('name')
                    if isinstance(value, list):
                        value = value[0] if value else None
                    if not value:
                        continue
                    name = html.unescape(str(value)).strip()
                    if name and name not in seen:
                        seen.add(name)
                        names.append(name)
                if len(rows) < self.CA_TASK_PAGE_SIZE:
                    break
                offset += self.CA_TASK_PAGE_SIZE
                # Courtesy gap between pages, as in scripts/sync_wiki_drops.py.
                await asyncio.sleep(1.0)
                if offset > 20000:
                    # Guard against a pagination bug (an ignored offset, a page
                    # that is always full) turning into a crawl. Return nothing
                    # rather than what we have: a truncated list is worse than
                    # no list, because the caller caches it for a week and
                    # every task past the cut then looks like it doesn't exist.
                    print("CA task fetch aborted: implausible row count")
                    return []
        except Exception as e:
            print(f"Error fetching combat achievement names: {e}")
            return []
        return names

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

    async def _nothing_drops_item(self, item_name: str) -> Optional[bool]:
        """Is this an item the game provably never drops?

        True only when the wiki HAS an ``infobox_item`` page under this name
        and every variant sharing the name is older than
        ``NEW_ITEM_GRACE_DAYS`` — so an empty dropsline result means "no
        monster drops this", not "nobody has documented it yet".

        False on any conclusive lookup short of that (name the wiki doesn't
        know, missing or unparseable release date), which keeps the caller on
        its fail-open path. None when the wiki couldn't answer at all
        (transport/API error) — same fail-open effect for the caller, but the
        caching layer must not remember it.
        """
        query = (
            "bucket('infobox_item')"
            ".select('release_date')"
            f".where('item_name', {self._bucket_quote(item_name)})"
            ".limit(50).run()"
        )
        result = await self._bucket_query(query)
        if 'bucket' not in result:
            return None

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

    async def _nothing_drops_item_cached(
        self, item_name: str, use_cache: bool
    ) -> tuple:
        """Cached wrapper for _nothing_drops_item.

        Returns ``(verdict, used_cache)``. The verdict caches for
        DROP_CACHE_TTL_SECONDS; errors (None) pass through uncached. A cached
        True verdict still gets revalidated live by check_drop before it can
        reject anything, so staleness only ever extends an allow.
        """
        key = f"wiki:nodrops:v1:{item_name.strip().lower()}"
        if use_cache:
            cached = self._cache_get(key)
            if isinstance(cached, dict) and "verdict" in cached:
                return bool(cached["verdict"]), True
        verdict = await self._nothing_drops_item(item_name)
        if verdict is not None:
            self._cache_set(key, {"verdict": verdict})
        return verdict, False

    async def _dropsline_sources(
        self, lookup_name: str, use_cache: bool
    ) -> tuple:
        """The wiki's drop-source page names for one dropsline item name.

        Returns ``(sources, used_cache)`` where sources is a list of raw
        page_name strings (possibly empty), or None when the wiki couldn't
        answer. Successful lookups — including empty ones — cache for
        DROP_CACHE_TTL_SECONDS; errors are never cached.
        """
        key = f"wiki:dropsline:v1:{lookup_name.strip().lower()}"
        if use_cache:
            cached = self._cache_get(key)
            if isinstance(cached, list):
                return cached, True

        # The explicit .limit() is load-bearing: see DROPSLINE_ROW_LIMIT.
        query = (
            "bucket('dropsline')"
            ".select('page_name')"
            f".where('item_name', {self._bucket_quote(lookup_name)})"
            f".limit({self.DROPSLINE_ROW_LIMIT}).run()"
        )
        result = await self._bucket_query(query)

        # `_bucket_query` returns a dict WITHOUT a 'bucket' key on transport
        # or API error (a successful query always includes 'bucket', even
        # when the list is empty), which lets us tell "the wiki couldn't
        # answer" apart from "the item has no drop sources".
        if 'bucket' not in result:
            return None, False

        sources = [str(e.get('page_name', '')) for e in result['bucket']]
        self._cache_set(key, sources)
        return sources, False

    async def check_drop(self, item_name: str, npc_name: str) -> bool:
        """
        Check if an item drops from a specific NPC.

        Wiki lookups are served from the (optional) cache for up to
        DROP_CACHE_TTL_SECONDS. Allows may therefore be decided from cached
        data, but a REJECT derived from any cached data is always re-run
        against the live wiki first — so caching can delay an allow for a
        newly-added drop source by at most the TTL, and can never cause a
        false rejection that live data wouldn't.

        Args:
            item_name: The name of the item
            npc_name: The name of the NPC

        Returns:
            True if the item drops from the NPC, False otherwise
        """
        try:
            allowed, used_cache = await self._check_drop_inner(
                item_name, npc_name, use_cache=True
            )
            if not allowed and used_cache:
                print(f"Cached data rejects {item_name} from {npc_name}; "
                      f"revalidating against the live wiki")
                allowed, _ = await self._check_drop_inner(
                    item_name, npc_name, use_cache=False
                )
            return allowed

        except Exception as e:
            # Any unexpected error (network, parsing, …) is inconclusive — the
            # caller's high-value verification also fails open, but we return
            # True here directly so callers that treat this as a plain bool
            # (scripts, tests) don't misread an error as a confident "no".
            print(f"Error checking drop for {item_name} from {npc_name}: {e}; allowing (fail-open)")
            return True

    async def _check_drop_inner(
        self, item_name: str, npc_name: str, use_cache: bool
    ) -> tuple:
        """One verification pass. Returns ``(allowed, used_cache)``.

        ``used_cache`` is True when any wiki data consulted came from the
        cache — check_drop uses it to decide whether a rejection needs a live
        revalidation pass. With ``use_cache=False`` every lookup hits the wiki
        and refreshes the cache.
        """
        # Handle special cases
        if item_name == "Enhanced crystal teleport seed" and npc_name == "Elf":
            return True, False
        if item_name.strip() == "Black tourmaline core" and npc_name.strip() == "Dusk":
            return True, False
        if npc_name.strip() == "Kingdom of Miscellania":
            # Kingdom resource collection is an activity, not a monster;
            # the dropsline bucket can never confirm it, so every stack
            # (coal, herbs, logs, nests) worth >1M would be a guaranteed
            # confident-negative false rejection.
            return True, False

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

        used_cache = False
        for lookup_name in lookup_names:
            sources, from_cache = await self._dropsline_sources(lookup_name, use_cache)
            used_cache = used_cache or from_cache

            # FAIL-OPEN on anything inconclusive. This check exists to block
            # spoofed high-value submissions, and a spoof can only be proven
            # POSITIVELY — the wiki lists real drop sources for the item and
            # this NPC isn't among them. Every other outcome (wiki
            # unavailable/rate-limited, malformed query, or the item simply
            # not indexed in `dropsline`) is *inconclusive* and must NOT be
            # treated as a spoof, or we silently reject legitimate drops.
            if sources is None:
                print(f"Dropsline lookup unavailable for {lookup_name!r}; allowing (fail-open)")
                return True, used_cache

            if not sources:
                # No rows under this name — try the next variant before
                # concluding the item isn't indexed at all.
                continue

            # Check if any of the returned NPCs match our target NPC
            for dropped_from in sources:
                # Remove any subpage references (e.g., "NPC name#Normal")
                if "#" in dropped_from:
                    dropped_from = dropped_from.split("#")[0]

                # Check if this drop source matches our NPC name (or an alias).
                if dropped_from.lower().strip() in acceptable_norm:
                    print(f"Drop found & valid for {item_name} from {dropped_from}")
                    return True, used_cache

            # A full page means the wiki had at least as many drop-source rows
            # as we asked for, so it may have more that it never sent — "not in
            # this list" then proves nothing. Inconclusive, so fail open.
            if len(sources) >= self.DROPSLINE_ROW_LIMIT:
                print(f"Dropsline results for {lookup_name!r} hit the "
                      f"{self.DROPSLINE_ROW_LIMIT}-row query limit; allowing (fail-open)")
                return True, used_cache

            # Confident negative: the item HAS known drop sources and this NPC
            # is not one of them. This is the only case we reject.
            print(f"No valid drop found for {item_name} from {npc_name} "
                  f"(matched dropsline name {lookup_name!r}; wiki sources: "
                  f"{sorted(set(sources))})")
            return False, used_cache

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
        verdict, from_cache = await self._nothing_drops_item_cached(item_name, use_cache)
        used_cache = used_cache or from_cache
        if verdict:
            print(f"Nothing in the game drops {item_name!r} (wiki-indexed item "
                  f"with no dropsline rows); rejecting the claimed "
                  f"{npc_name} source")
            return False, used_cache

        print(f"No dropsline data for {item_name!r}; allowing (fail-open)")
        return True, used_cache
    
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

        Note: a failed/partial wiki lookup returns (0.0, 0) here, which reads as
        "no progress" rather than "unknown". Anything user-facing should go
        through ``services.ca_tiers`` instead — it caches the thresholds,
        rejects partial answers and falls back to pinned values.
        
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

        Returns None both for "below Easy" and for "the wiki lookup failed" —
        see the note on get_ca_tier_progress; prefer ``services.ca_tiers``.
        
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
