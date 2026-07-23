import os
import asyncio
import functools
import json
import logging
import time
from dotenv import load_dotenv
from db import Player, session, models

import wom
from wom import Err, Result
from utils.format import normalize_player_display_equivalence
from utils.redis import redis_client
from typing import Optional, Dict, Any, List, Tuple
load_dotenv()

logger = logging.getLogger("utils.wiseoldman")
logger.setLevel(logging.INFO)

# The API server runs 6 hypercorn worker processes (see droptracker-api.service),
# plus the Discord bot and assorted scripts, all importing this module independently.
# A per-process rate limiter / cache (the old approach) gives each of those processes
# its own independent budget, so the *effective* combined rate to WOM was N times
# what's configured here. Backing the limiter and the player/group caches with Redis
# makes them a single shared budget/cache across every process on the box.
WOM_RATE_LIMIT_REQUESTS = int(os.getenv("WOM_RATE_LIMIT_REQUESTS", "100"))
WOM_RATE_LIMIT_PERIOD_SECONDS = float(os.getenv("WOM_RATE_LIMIT_PERIOD_SECONDS", "65"))
# If the next free slot is further away than this, callers do NOT queue: the
# lookup fails fast and the caller falls back to its WOM-unavailable path.
# Without this bound, sustained demand above the rate limit builds an unbounded
# FIFO of sleeping coroutines (observed: submission handlers stuck 7+ hours).
WOM_RATE_LIMIT_MAX_WAIT_SECONDS = float(os.getenv("WOM_RATE_LIMIT_MAX_WAIT_SECONDS", "15"))
_WOM_RATE_LIMIT_KEY = "wom:rate:next_slot"
_WOM_RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local max_wait = tonumber(ARGV[3])
local next_slot = tonumber(redis.call('GET', key))
if not next_slot or next_slot < now then
    next_slot = now
end
if next_slot - now > max_wait then
    return '-1'
end
redis.call('SET', key, tostring(next_slot + interval), 'PX', math.ceil((next_slot + interval - now) * 1000) + 1000)
return tostring(next_slot)
"""


class _SharedRateLimiter:
    """Distributed leaky-bucket limiter backed by Redis.

    Every process that talks to WOM reserves the next available time slot via
    a single atomic Redis command, so the whole application shares one request
    budget instead of each process enforcing its own.
    """

    def __init__(self, requests: int, period_seconds: float, max_wait_seconds: float):
        self._interval = period_seconds / requests
        self._max_wait = max_wait_seconds

    async def wait(self) -> bool:
        """Reserve a slot and sleep until it arrives.

        Returns False (without sleeping or consuming budget) when the backlog
        already exceeds max_wait -- the caller should skip the WOM call.
        """
        now = time.time()
        try:
            reserved = float(
                redis_client.client.eval(
                    _WOM_RATE_LIMIT_LUA, 1, _WOM_RATE_LIMIT_KEY,
                    now, self._interval, self._max_wait,
                )
            )
        except Exception as e:
            logger.warning("WOM shared rate limiter unavailable (%s); proceeding unthrottled", e)
            return True
        if reserved < 0:
            logger.warning("WOM rate limit backlog exceeds %.0fs; skipping call", self._max_wait)
            return False
        delay = reserved - now
        if delay > 0:
            await asyncio.sleep(delay)
        return True


limiter = _SharedRateLimiter(
    WOM_RATE_LIMIT_REQUESTS, WOM_RATE_LIMIT_PERIOD_SECONDS, WOM_RATE_LIMIT_MAX_WAIT_SECONDS
)

# Fetch the WOM_API_KEY from environment variables
WOM_API_KEY = os.getenv("WOM_API_KEY")

# Initialize the WOM Client with API key and user agent
client = wom.Client(
    WOM_API_KEY,
    user_agent="@joelhalen"
)

PLAYER_CACHE_TTL = int(os.getenv("WOM_PLAYER_CACHE_TTL", "900"))
PLAYER_FAIL_CACHE_TTL = int(os.getenv("WOM_PLAYER_FAIL_CACHE_TTL", "300"))
GROUP_CACHE_TTL = int(os.getenv("WOM_GROUP_CACHE_TTL", "900"))

# Player/group lookup caches also live in Redis (see _SharedRateLimiter docstring above)
# so a cache hit in one API worker is a cache hit for all of them.
_REDIS_PLAYER_PREFIX = "wom:player:"
_REDIS_PLAYER_FAIL_PREFIX = "wom:player:fail:"
_REDIS_GROUP_PREFIX = "wom:group:"

# Stores the WOM-reported total member_count per group (populated during API calls).
# Used by _sync_group_from_wom to detect incomplete API responses before removing members.
# This one stays in-process: it's only read within the same request that populates it
# (update_group_members runs in the single-process Discord bot, not the API workers).
_group_member_count: Dict[int, int] = {}


def _extract_group_metadata(details: Any) -> Tuple[Optional[str], Optional[int]]:
    """
    Normalize group metadata across WOM response shapes.

    Depending on wom.py version, group details may be exposed either directly on the
    returned object (`details.name`, `details.member_count`) or nested under a
    `details.group` object (`details.group.name`, `details.group.member_count`).
    """
    nested_group = getattr(details, "group", None)
    group_name = getattr(nested_group, "name", None)
    member_count = getattr(nested_group, "member_count", None)

    if group_name is None:
        group_name = getattr(details, "name", None)
    if member_count is None:
        member_count = getattr(details, "member_count", None)

    try:
        member_count = int(member_count) if member_count is not None else None
    except (TypeError, ValueError):
        member_count = None

    return group_name, member_count

async def _get_cached_player(cache_key: str, force_refresh: bool) -> Optional[Tuple]:
    if force_refresh:
        return None
    normalized = cache_key.strip().lower()
    try:
        raw = redis_client.client.get(_REDIS_PLAYER_PREFIX + normalized)
        if raw is not None:
            return tuple(json.loads(raw))
        raw_fail = redis_client.client.get(_REDIS_PLAYER_FAIL_PREFIX + normalized)
        if raw_fail is not None:
            return (None, None, None, -1)
    except Exception as e:
        logger.debug("WOM player cache read failed for %s: %s", normalized, e)
    return None


async def _store_player_cache(cache_key: str, value: Tuple, success: bool):
    normalized = cache_key.strip().lower()
    try:
        if success:
            redis_client.client.setex(
                _REDIS_PLAYER_PREFIX + normalized, PLAYER_CACHE_TTL, json.dumps(list(value))
            )
            redis_client.client.delete(_REDIS_PLAYER_FAIL_PREFIX + normalized)
        else:
            redis_client.client.setex(_REDIS_PLAYER_FAIL_PREFIX + normalized, PLAYER_FAIL_CACHE_TTL, "1")
    except Exception as e:
        logger.debug("WOM player cache write failed for %s: %s", normalized, e)


async def _get_cached_group(wom_group_id: int, force_refresh: bool) -> Optional[List[int]]:
    if force_refresh:
        return None
    try:
        raw = redis_client.client.get(_REDIS_GROUP_PREFIX + str(wom_group_id))
        if raw is not None:
            return json.loads(raw)
    except Exception as e:
        logger.debug("WOM group cache read failed for %s: %s", wom_group_id, e)
    return None


async def _store_group_cache(wom_group_id: int, value: List[int]):
    try:
        redis_client.client.setex(_REDIS_GROUP_PREFIX + str(wom_group_id), GROUP_CACHE_TTL, json.dumps(value))
    except Exception as e:
        logger.debug("WOM group cache write failed for %s: %s", wom_group_id, e)


def _extract_log_slots(player) -> int:
    """Read the collection-log slot count out of a WOM player detail object."""
    snapshot = getattr(player, "latest_snapshot", None)
    if not snapshot:
        return -1
    snapshot_data = getattr(snapshot, "data", None)
    if not snapshot_data:
        return 0
    activities = getattr(snapshot_data, "activities", {})
    for activity_name, activity_obj in activities.items():
        if str(activity_name).split(".")[-1].lower() == "collections_logged":
            return getattr(activity_obj, "score", -1)
    return 0


def _total_level_from_raw_player(player) -> int:
    try:
        overall = player.latest_snapshot.data.skills.get("overall")
        return int(overall.level)
    except Exception:
        return 0


def _ehb_from_raw_player(player):
    """Top-level efficient-hours-bossed off a WOM player detail object, or
    None when absent/garbage (None = unknown, distinct from a real 0.0)."""
    try:
        ehb = getattr(player, "ehb", None)
        return round(float(ehb), 2) if ehb is not None else None
    except (TypeError, ValueError):
        return None


def _identity_shim(player) -> dict:
    """The lightweight identity dict cached + returned by check_user_by_*.
    Extending it is backward-compatible: consumers .get() what they know."""
    return {
        "total_level": _total_level_from_raw_player(player),
        "ehb": _ehb_from_raw_player(player),
    }


def _log_wom_call(action: str, **kwargs):
    if logger.isEnabledFor(logging.INFO):
        logger.info("WOM API call: %s %s", action, kwargs)

async def check_user_by_username(username: str, *, force_refresh: bool = False) -> tuple[Any, str, int, int]:
    """ Check a user in the WiseOldMan database, returning a lightweight identity
        shim (dict with a `total_level` key, or None), their WOM ID, and their
        displayName.
        Returns (identity, player_name, player_wom_id, log_slots)
    """
    # Cached results (success or failure) prevent hammering the API for unchanged data.
    # This cache is shared (Redis) across every process talking to WOM.
    cached = await _get_cached_player(username, force_refresh)
    if cached is not None:
        return cached

    if not await limiter.wait():
        # Budget exhausted: don't fail-cache (the player is fine, we're just
        # throttled); callers fall back to local identity resolution.
        return None, None, None, -1
    await client.start()  # Initialize the client (if required by the `wom` library)
    try:
        _log_wom_call("players.get_details", username=username, force_refresh=force_refresh)
        result = await client.players.get_details(username=username)
        if result.is_ok:
            player = result.unwrap()
            if player is None:
                payload = (None, None, None, -1)
                await _store_player_cache(username, payload, success=False)
                return payload
            log_slots = _extract_log_slots(player)
            identity = _identity_shim(player)
            payload = (identity, player.username, player.id, log_slots)
            await _store_player_cache(username, payload, success=True)
            return payload

        error = result.unwrap_err()
        status = getattr(error, "status", None)
        if status != 404:
            # Only a genuine "player not found" warrants the heavier update_player
            # call (which asks WOM to re-scrape hiscores for a brand new account).
            # Falling back to it on *any* failure -- including 429/5xx -- used to
            # double our call volume during exactly the moments WOM was already
            # struggling to keep up with us.
            logger.info("WOM get_details failed for %s (status=%s); not retrying via update_player", username, status)
            payload = (None, None, None, -1)
            await _store_player_cache(username, payload, success=False)
            return payload

        try:
            _log_wom_call("players.update_player", username=username)
            result = await client.players.update_player(username=username)
        except Exception as e:
            print("Error updating player:", e)
            payload = (None, None, None, -1)
            await _store_player_cache(username, payload, success=False)
            return payload

        if not result.is_ok:
            print(f"Update player failed for {username}.")
            payload = (None, None, None, -1)
            await _store_player_cache(username, payload, success=False)
            return payload

        player = result.unwrap()
        if player is None:
            print(f"Got empty player object after update for {username}")
            payload = (None, None, None, -1)
            await _store_player_cache(username, payload, success=False)
            return payload

        log_slots = _extract_log_slots(player)
        identity = _identity_shim(player)
        payload = (identity, str(player.username), str(player.id), log_slots)
        await _store_player_cache(username, payload, success=True)
        return payload
    except Exception as e:
        print(f"Error checking user {username}: {str(e)}")
        payload = (None, None, None, -1)
        await _store_player_cache(username, payload, success=False)
        return payload

async def check_user_by_id(uid: int, *, force_refresh: bool = False):
    """ Check a user in the WiseOldMan database, returning a lightweight identity
        shim (dict with a `total_level` key, or None), their WOM ID, and their
        displayName.
        Returns (identity, player_name, player_wom_id, log_slots)
    """
    cache_key = f"id:{uid}"
    cached = await _get_cached_player(cache_key, force_refresh)
    if cached is not None:
        return cached

    if not await limiter.wait():
        return None, None, None, -1
    await client.start()  # Initialize the client (if required by the `wom` library)

    try:
        _log_wom_call("players.get_details_by_id", player_id=uid, force_refresh=force_refresh)
        result = await client.players.get_details_by_id(player_id=uid)
        if result.is_ok:
            player = result.unwrap()
            if player is None:
                payload = (None, None, None, -1)
                await _store_player_cache(cache_key, payload, success=False)
                return payload
            log_slots = _extract_log_slots(player)
            identity = _identity_shim(player)
            payload = (identity, str(player.username), str(player.id), log_slots)
            await _store_player_cache(cache_key, payload, success=True)
            await _store_player_cache(player.username, payload, success=True)
            return payload
        else:
            # Handle the case where the request failed
            payload = (None, None, None, -1)
            await _store_player_cache(cache_key, payload, success=False)
            return payload
    except Exception as e:
        print(f"Error checking user by id {uid}: {str(e)}")
        return None, None, None, -1

async def check_group_by_id(wom_group_id: int, *, force_refresh: bool = False):
    """ Searches for a group on WiseOldMan by a passed group ID 
        Returns group_name, member_count and members (list)    
    """
    wom_id = str(wom_group_id)
    if not await limiter.wait():
        return None, None, None
    await client.start()
    try:
        _log_wom_call("groups.get_details", id=wom_id, force_refresh=force_refresh)
        result = await client.groups.get_details(id=wom_id)
        if result.is_ok:
            details = result.unwrap()
            members = details.memberships
            group_name, member_count = _extract_group_metadata(details)
            member_ids = [member.player_id for member in members]
            await _store_group_cache(wom_group_id, member_ids)
            return group_name, member_count, members
        else:
            return None, None, None
    finally:
        pass

def _autoclean_scoped_session_async(fn):
    """Async analogue of ``services.points._autoclean_scoped_session``.

    ``fetch_group_members`` falls back to the module-level *scoped* session
    (``models.session``) when the caller passes no session. Its reads autobegin
    a transaction that several code paths never commit or roll back, so the
    scoped session keeps its connection checked out with an idle read
    transaction — the 2026-07-15 idle-transaction leak, here in the bot /
    player-updates / intake processes (webapi already has a request teardown).

    When the caller owns the session (passes ``session_to_use=...``) we touch
    nothing; cleanup is theirs. When we fell back to the scoped session we
    ``rollback()`` it in a ``finally``: that ends the idle transaction and
    returns the connection to the pool. We roll back rather than
    ``Session.remove()`` on purpose — callers such as
    ``db.ops._sync_group_from_wom`` share this same scoped session and keep live
    ORM objects across the call, then read relationships on them afterwards
    (e.g. ``group.players``); ``remove()`` would detach those objects and raise
    ``DetachedInstanceError``, while ``rollback()`` releases the connection
    without detaching. Any writes made above (name corrections, provisioned
    players) are committed inline before this runs, so the trailing rollback
    only clears the leftover read transaction.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        # ``session_to_use`` is the 2nd positional parameter; the call owns the
        # scoped session only when no session was supplied by either route.
        supplied = args[1] if len(args) >= 2 else kwargs.get("session_to_use", None)
        if supplied is not None:
            return await fn(*args, **kwargs)
        try:
            return await fn(*args, **kwargs)
        finally:
            try:
                models.session.rollback()
            except Exception:
                pass
    return wrapper


@_autoclean_scoped_session_async
async def fetch_group_members(
    wom_group_id: int,
    session_to_use = None,
    *,
    force_refresh: bool = False,
    use_cache: bool = True,
    provision_missing: bool = False,
):
    """
    Returns a list of WiseOldMan Player IDs
    for members of a specified group
    """
    #print("Fetching group members for ID:", wom_group_id)
    user_list = []
    if session_to_use is not None:
        session = session_to_use
    else:
        session = models.session
    
    if not force_refresh:
        # Attempt to satisfy from local database first
        db_session = session_to_use or models.session
        try:
            group_obj = (
                db_session.query(models.Group)
                .filter(models.Group.wom_id == wom_group_id)
                .first()
            )
            if group_obj:
                local_members = (
                    db_session.query(models.Player.wom_id)
                    .join(
                        models.user_group_association,
                        (models.user_group_association.c.player_id == models.Player.player_id),
                    )
                    .filter(
                        models.user_group_association.c.group_id == group_obj.group_id,
                        models.Player.wom_id != None,
                    )
                    .all()
                )
                if local_members:
                    return [member.wom_id for member in local_members if member.wom_id is not None]
        except Exception as db_ex:
            logger.debug("Local member lookup failed for WOM group %s: %s", wom_group_id, db_ex)
    if wom_group_id == 1:
        # Fetch all player WOM IDs from the database directly
        players = session.query(Player.wom_id).all()
        # Unpack the list of tuples returned by SQLAlchemy
        user_list = [player.wom_id for player in players] 
        return user_list
    cached = await _get_cached_group(wom_group_id, force_refresh or not use_cache)
    if cached is not None:
        return cached
    if not await limiter.wait():
        return []
    await client.start()
    try:
        _log_wom_call("groups.get_details", id=wom_group_id, force_refresh=force_refresh)
        result = await client.groups.get_details(wom_group_id)
        if result.is_ok:
            details = result.unwrap()
            members = details.memberships
            # Store the WOM-reported expected member count so _sync_group_from_wom
            # can detect and refuse to act on incomplete API responses.
            group_name, wom_expected_count = _extract_group_metadata(details)
            if wom_expected_count is not None:
                _group_member_count[wom_group_id] = wom_expected_count
            name = group_name
            #print(f"Group name: {name}")
            ehb_updates = 0
            for member in members:
                player_obj = getattr(member, "player", None)
                player_name = getattr(player_obj, "display_name", None)
                member_wom_id = member.player_id
                existing_player = session.query(Player).filter(Player.wom_id == member_wom_id).first()
                if existing_player:
                    # Every membership carries a full WOM player object, so this
                    # already-paid-for hourly call refreshes EHB for the whole
                    # clan at no extra API cost (previously discarded). None is
                    # "unknown" and never overwrites a stored value; the write is
                    # left pending and committed once after the loop.
                    member_ehb = _ehb_from_raw_player(player_obj)
                    if member_ehb is not None and (
                        existing_player.ehb is None
                        or float(existing_player.ehb) != member_ehb
                    ):
                        existing_player.ehb = member_ehb
                        ehb_updates += 1

                    old_name = existing_player.player_name or ""
                    new_name = player_name or ""
                    # Only update if the names differ beyond hyphen/underscore vs space changes
                    if normalize_player_display_equivalence(old_name) != normalize_player_display_equivalence(new_name):
                        if old_name != new_name:
                            print(f"Updated player name for {old_name} to {new_name}")
                            existing_player.player_name = new_name
                            session.commit()
                else:
                    # No player found by WOM ID. A stale wom_id (e.g. after an RSN change
                    # that created a new WOM identity) would cause the sync to incorrectly
                    # remove a valid group member. Try matching by player name to detect and
                    # correct this before the removal pass runs.
                    player_by_name = None
                    if player_name:
                        # Normalised, ghost-excluding lookup. OSRS names are
                        # space/underscore/hyphen-insensitive, so exact-string
                        # matching missed "lm_Brad" vs "lm Brad" and minted a
                        # duplicate wom_temp twin. Reusing the existing real row
                        # here is what prevents the split-identity bug.
                        player_by_name = _find_real_player_by_normalized_name(session, player_name)
                        if player_by_name is not None and player_by_name.wom_id != member_wom_id:
                            logger.info(
                                "Correcting stale WOM ID for player '%s': %s -> %s",
                                player_name, player_by_name.wom_id, member_wom_id,
                            )
                            player_by_name.wom_id = member_wom_id
                            try:
                                session.commit()
                            except Exception as commit_err:
                                logger.warning(
                                    "Failed to correct WOM ID for '%s': %s",
                                    player_name, commit_err,
                                )
                                session.rollback()
                    if player_by_name is None and provision_missing:
                        # Player has never used the plugin — create a stub with a temporary
                        # account hash so they can be tracked in groups right away.
                        _create_player_from_wom_member(session, member_wom_id, player_name, player_obj)
                user_list.append(member_wom_id)
            if ehb_updates:
                # One commit for the whole clan rather than per member. EHB is a
                # side effect of the membership sync: a failure here must never
                # cost the caller its member list (the add/remove pass depends
                # on it), so it's logged and swallowed.
                try:
                    session.commit()
                    logger.info("Refreshed EHB for %s member(s) of WOM group %s",
                                ehb_updates, wom_group_id)
                except Exception as ehb_err:
                    logger.warning("EHB refresh commit failed for WOM group %s: %s",
                                   wom_group_id, ehb_err)
                    session.rollback()
            await _store_group_cache(wom_group_id, user_list)
            return user_list
        else:
            return []
    except Exception as e:
        print("Couldn't find WOM group members... Error:", e)
        return []

def _find_real_player_by_normalized_name(db_session, player_name: Optional[str]) -> Optional[Player]:
    """Return the single non-ghost Player whose display name is OSRS-equivalent
    to ``player_name`` (space/underscore/hyphen- and case-insensitive), or None.

    OSRS treats spaces, underscores and hyphens as equivalent in display names,
    so "lm_Brad" and "lm Brad" are the same account. WOM-import stubs
    (account_hash ``wom_temp_*``) are excluded so we only ever reuse a real,
    plugin-authed identity. Returns None unless EXACTLY ONE non-ghost row
    matches, so a recycled RSN shared by two different accounts is never
    silently collapsed onto the wrong one.
    """
    if not player_name:
        return None
    target = normalize_player_display_equivalence(player_name)
    # Cheap candidate net: exact plus space/underscore/hyphen swaps. The
    # authoritative equivalence check is done in Python below.
    variants = {player_name}
    for a in (" ", "_", "-"):
        for b in (" ", "_", "-"):
            variants.add(player_name.replace(a, b))
    candidates = (
        db_session.query(Player)
        .filter(Player.player_name.in_(list(variants)))
        .all()
    )
    real_matches = [
        p for p in candidates
        if normalize_player_display_equivalence(p.player_name or "") == target
        and not str(p.account_hash or "").startswith("wom_temp_")
    ]
    return real_matches[0] if len(real_matches) == 1 else None


def _create_player_from_wom_member(db_session, wom_id: int, player_name: Optional[str], player_obj=None) -> Optional[Player]:
    """Create a Player record for a WOM group member with no local account.

    The temporary account hash (wom_temp_<wom_id>) is replaced automatically
    when the player first authenticates via the RuneLite plugin.
    """
    temp_hash = f"wom_temp_{wom_id}"
    existing = db_session.query(Player).filter(Player.account_hash == temp_hash).first()
    if existing:
        return existing
    # Never mint a duplicate stub for an account that already exists under its
    # real (plugin-authed) identity — reuse it so clan memberships attach to the
    # row that actually receives drops rather than to an orphan ghost. Bounded to
    # a unique non-ghost name match so a recycled RSN can't merge two accounts.
    real_twin = _find_real_player_by_normalized_name(db_session, player_name)
    if real_twin is not None:
        logger.info("Reusing existing player '%s' (id=%s) for WOM member wom_id=%s instead of a wom_temp stub",
                    real_twin.player_name, real_twin.player_id, wom_id)
        return real_twin

    total_level = 0
    try:
        if player_obj and getattr(player_obj, "latest_snapshot", None):
            overall = player_obj.latest_snapshot.data.skills.get("overall")
            if overall:
                total_level = overall.level
    except Exception:
        pass

    name = player_name or f"Unknown_{wom_id}"
    new_player = Player(
        wom_id=wom_id,
        player_name=name,
        account_hash=temp_hash,
        total_level=total_level,
        log_slots=0,
        ehb=_ehb_from_raw_player(player_obj),
    )
    db_session.add(new_player)
    try:
        db_session.commit()
        logger.info("Created WOM-imported player '%s' (wom_id=%s) with temporary hash", name, wom_id)
        return new_player
    except Exception as e:
        logger.warning("Failed to create WOM-imported player '%s' (wom_id=%s): %s", name, wom_id, e)
        db_session.rollback()
        return None


async def get_collections_logged(username: str):
    """
    Returns an integer representation of the number of collection 
    log slots a player has unlocked according to WiseOldMan
    """
    if not await limiter.wait():
        return -1
    await client.start()
    _log_wom_call("players.get_details", username=username)
    player_data = await client.players.get_details(username=username)
    if player_data.is_ok:
        details = player_data.unwrap()
        snapshot = getattr(details, "latest_snapshot", None)
        if snapshot:
            snapshot_data = getattr(snapshot, "data", None)
        else:
            return -1
        if snapshot_data:
            activities = getattr(snapshot_data, "activities", {})
            for activity_name, activity_obj in activities.items():
                activity_name_str = str(activity_name).split(".")[-1].lower()
                score = getattr(activity_obj, "score", -1)
                if activity_name_str == "collections_logged":
                    return score
        else:
            return 0
    else:
        return -1
    
def get_player_total_kills(wom_id: int):
    loop = asyncio.get_event_loop()
    if loop.is_running():
        future = asyncio.run_coroutine_threadsafe(get_player_total_kills(wom_id), loop)
        return future.result()
    else:
        return loop.run_until_complete(get_player_total_kills(wom_id))
    
async def get_player_total_kills(wom_id: int):
    if not await limiter.wait():
        return None
    await client.start()
    _log_wom_call("players.get_details_by_id", player_id=wom_id)
    player_data = await client.players.get_details_by_id(player_id=wom_id)
    if player_data.is_ok:
        details = player_data.unwrap()
        snapshot = getattr(details, "latest_snapshot", None)
        if snapshot is not None:
            snapshot_data = getattr(snapshot, "data", None)
            if snapshot_data is not None:
                bosses = getattr(snapshot_data, "bosses", {})
                for boss_name, boss_obj in bosses.items():
                    kills = getattr(boss_obj, "kills", -1)
                    if kills > 0:
                        return kills

def get_player_boss_kills_sync(username: str, boss_metric: str) -> Optional[int]:
    """
    Synchronous helper to fetch boss kill count by username.
    Returns int kills if available, 0 if boss not found or non-positive, or None on error.
    """
    loop = asyncio.get_event_loop()
    if loop.is_running():
        future = asyncio.run_coroutine_threadsafe(get_player_boss_kills(username, boss_metric), loop)
        return future.result()
    else:
        return loop.run_until_complete(get_player_boss_kills(username, boss_metric))

async def get_player_boss_kills(username: str, boss_metric: str) -> Optional[int]:
    """
    Return the kill count (int) for the specified boss metric for a given username.
    - Returns 0 if the boss is present but has no recorded kills or not found in the snapshot.
    - Returns None if data cannot be retrieved (API error or missing snapshot data).
    """
    if not await limiter.wait():
        return None
    await client.start()
    try:
        _log_wom_call("players.get_details", username=username)
        player_data: Result = await client.players.get_details(username=username)
        return await _extract_boss_kills_from_player_result(player_data, boss_metric)
    except Exception:
        return None

async def get_player_boss_kills_by_id(wom_id: int, boss_metric: str) -> Optional[int]:
    """
    Return the kill count (int) for the specified boss metric for a given WOM player id.
    - Returns 0 if the boss is present but has no recorded kills or not found in the snapshot.
    - Returns None if data cannot be retrieved (API error or missing snapshot data).
    """
    if not await limiter.wait():
        return None
    await client.start()
    try:
        _log_wom_call("players.get_details_by_id", player_id=wom_id)
        player_data: Result = await client.players.get_details_by_id(wom_id)
        return await _extract_boss_kills_from_player_result(player_data, boss_metric)
    except Exception:
        return None

async def _extract_boss_kills_from_player_result(player_data: Result, boss_metric: str) -> Optional[int]:
    """
    Internal helper to normalize boss metric name and extract kills from a player Result.
    Returns int kills, 0 if not found/non-positive, or None if data unavailable.
    """
    normalized_target = (
        boss_metric.strip().lower().replace(" ", "_").replace("-", "_").replace("'", "")
    )
    if not player_data or not getattr(player_data, "is_ok", False):
        return None
    details = player_data.unwrap()
    snapshot = getattr(details, "latest_snapshot", None)
    if not snapshot:
        return None
    snapshot_data = getattr(snapshot, "data", None)
    if not snapshot_data:
        return None
    bosses = getattr(snapshot_data, "bosses", {}) or {}
    # Iterate bosses and match normalized metric key
    for boss_name, boss_obj in bosses.items():
        boss_key = str(boss_name).split(".")[-1].lower()
        if boss_key == normalized_target:
            kills = getattr(boss_obj, "kills", -1)
            try:
                kills_int = int(kills)
            except Exception:
                return 0
            return kills_int if kills_int > 0 else 0
    # If boss key didn't match any entry, return 0 per spec
    return 0

def get_player_metric_sync(username: str, metric_name: str):
    """
    Returns an integer representation of a player's metric according to WiseOldMan
    using the existing event loop
    """
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # Create a future in the running loop
        future = asyncio.run_coroutine_threadsafe(get_player_metric(username, metric_name), loop)
        return future.result()  # This blocks until the result is available
    else:
        # If no loop is running, we can use loop.run_until_complete
        return loop.run_until_complete(get_player_metric(username, metric_name))

async def get_player_metric_by_id(wom_id: int, metric_name: str):
    """
    Returns an integer representation of a player's metric according to WiseOldMan
    """
    if not await limiter.wait():
        return -1
    await client.start()
    _log_wom_call("players.get_details_by_id", player_id=wom_id)
    player_data = await client.players.get_details_by_id(wom_id)
    return await _get_player_metric(player_data, metric_name)

async def _get_player_metric(player_data: Result, metric_name: str):
    metric_name = metric_name.replace(" ", "_").replace("'", "")
    if player_data.is_ok:
        details = player_data.unwrap()
        snapshot = getattr(details, "latest_snapshot", None)
        player_info = {
            "id": getattr(details, "id", None),
            "username": getattr(details, "username", None),
            "display_name": getattr(details, "display_name", None),
            "type": str(getattr(details, "type", None)),
            "build": str(getattr(details, "build", None)),
            "status": str(getattr(details, "status", None)),
            "combat_level": getattr(details, "combat_level", None),
            "exp": getattr(details, "exp", None),
            "ehp": getattr(details, "ehp", None),
            "ehb": getattr(details, "ehb", None),
            "ttm": getattr(details, "ttm", None),
            "tt200m": getattr(details, "tt200m", None),
            "registered_at": str(getattr(details, "registered_at", None)),
            "updated_at": str(getattr(details, "updated_at", None)),
            "last_changed_at": str(getattr(details, "last_changed_at", None))
        }
        if metric_name in player_info:
            return player_info[metric_name]
        skills_data = {}
        snapshot = getattr(details, "latest_snapshot", None)
        if snapshot:
            snapshot_data = getattr(snapshot, "data", None)
            if snapshot_data:
                skills = getattr(snapshot_data, "skills", {})
                for skill_name, skill_obj in skills.items():
                    skill_name_str = str(skill_name).split(".")[-1].lower()
                    skills_data[skill_name_str] = {
                        "level": getattr(skill_obj, "level", 0),
                        "experience": getattr(skill_obj, "experience", 0),
                        "rank": getattr(skill_obj, "rank", 0),
                        "ehp": getattr(skill_obj, "ehp", 0)
                    }
            if metric_name in skills_data:
                return skills_data[metric_name]
        boss_data = {}
        if snapshot and snapshot_data:
            bosses = getattr(snapshot_data, "bosses", {})
            print("Got bosses: " + str(bosses))
            for boss_name, boss_obj in bosses.items():
                kills = getattr(boss_obj, "kills", -1)
                if kills > 0:
                    boss_name_str = str(boss_name).split(".")[-1].lower()
                    boss_data[boss_name_str] = {
                        "kills": kills,
                        "rank": getattr(boss_obj, "rank", 0),
                        "ehb": getattr(boss_obj, "ehb", 0)
                    }
        
        if metric_name.lower() in [boss.lower() for boss in boss_data]:
            boss_data_obj = boss_data[metric_name.lower()]
            return {"kills": boss_data_obj["kills"]}
            # Extract activity data - include all activities
        activity_data = {}
        if snapshot and snapshot_data:
            activities = getattr(snapshot_data, "activities", {})
            for activity_name, activity_obj in activities.items():
                activity_name_str = str(activity_name).split(".")[-1].lower()
                score = getattr(activity_obj, "score", -1)
                activity_data[activity_name_str] = {
                    "score": score,
                    "rank": getattr(activity_obj, "rank", 0)
                }
        if metric_name in activity_data:
            return activity_data[metric_name]
        computed_data = {}
        if snapshot and snapshot_data:
            computed = getattr(snapshot_data, "computed", {})
            for metric_name, metric_obj in computed.items():
                metric_name_str = str(metric_name).split(".")[-1].lower()
                computed_data[metric_name_str] = {
                    "value": getattr(metric_obj, "value", 0),
                    "rank": getattr(metric_obj, "rank", 0)
                }
        if metric_name in computed_data:
            return computed_data[metric_name]
        else:
            return -1
    return -1

async def get_player_metric(username: str, metric_name: str):
    """
    Returns an integer representation of a player's metric according to WiseOldMan
    """
    if not await limiter.wait():
        return -1
    await client.start()
    _log_wom_call("players.get_details", username=username)
    player_data = await client.players.get_details(username=username)
    return await _get_player_metric(player_data, metric_name)

async def get_player_wom_data(username: str):
    """
    Returns a player object from WiseOldMan
    """
    if not await limiter.wait():
        return None
    await client.start()
    _log_wom_call("players.get_details", username=username)
    player_data = await client.players.get_details(username=username)
    return player_data

async def get_player_all_skills(username: str):
    """
    Returns all skills and their experience points for a player according to WiseOldMan
    Returns a dictionary with skill names as keys and experience points as values
    """
    if not await limiter.wait():
        return {}
    await client.start()
    _log_wom_call("players.get_details", username=username)
    player_data = await client.players.get_details(username=username)

    if player_data.is_ok:
        details = player_data.unwrap()
        snapshot = getattr(details, "latest_snapshot", None)
        
        if snapshot:
            snapshot_data = getattr(snapshot, "data", None)
            if snapshot_data:
                skills = getattr(snapshot_data, "skills", {})
                skills_data = []
                
                for skill_name, skill_obj in skills.items():
                    skill_name_str = str(skill_name).split(".")[-1].lower()
                    skills_data.append({
                        f"{skill_name_str}": getattr(skill_obj, "experience", 0)
                    })
                
                return skills_data
    
    return {}

# ---------------------------------------------------------------------------
# Event reconciliation: group bulk endpoints + metric mapping
# ---------------------------------------------------------------------------
# wom.py 1.0.0 predates /bulk-gained and /bulk-hiscores, so these go through
# its custom-route layer and return plain parsed JSON (lists of dicts) rather
# than wom.py model objects.
from wom import routes as _wom_routes

_BULK_GAINED_ROUTE = _wom_routes.Route("GET", "/groups/{}/bulk-gained")
_BULK_HISCORES_ROUTE = _wom_routes.Route("GET", "/groups/{}/bulk-hiscores")

# Must stay below the reconcile cycle (WOM_RECONCILE_SECONDS) or polls would
# read their own cached previous response; it exists to collapse
# multi-event/same-group fetches and restart bursts.
WOM_BULK_CACHE_TTL = int(os.getenv("WOM_BULK_CACHE_TTL", "240"))
_REDIS_BULK_GAINED_PREFIX = "wom:bulkgained:"
_REDIS_BULK_HISCORES_PREFIX = "wom:bulkhiscores:"
UPDATE_ALL_BADCODE_PREFIX = "wom:updateall:badcode:"

# Skill/boss slug sets for family separation in metric mapping below. Single
# source of truth is the wom.py enum — and we pin the DropTracker fork (see
# requirements.txt / deploy/wom/README.md), which is kept current with the live
# WOM API, so no second hand-maintained list is needed here. If a brand-new
# boss/skill is missing, mapping just returns None for it (WOM-hybrid event
# tracking falls back to plugin-only, logged by the reconciler) — never a crash.
try:
    _WOM_SKILL_SLUGS = {m.value for m in wom.Skills}
    _WOM_BOSS_SLUGS = {m.value for m in wom.Bosses}
except Exception:  # enum shape changed — validation degrades to pass-through
    _WOM_SKILL_SLUGS = set()
    _WOM_BOSS_SLUGS = set()

_SKILL_METRIC_OVERRIDES = {"runecraft": "runecrafting"}

# Task targets / plugin NPC names that don't normalize onto WOM's boss slugs.
_BOSS_METRIC_OVERRIDES = {
    "barrows": "barrows_chests",
    "the_nightmare": "nightmare",
    "gauntlet": "the_gauntlet",
    "crystalline_hunllef": "the_gauntlet",
    "corrupted_gauntlet": "the_corrupted_gauntlet",
    "corrupted_hunllef": "the_corrupted_gauntlet",
    "tombs_of_amascut_expert_mode": "tombs_of_amascut_expert",
    "hueycoatl": "the_hueycoatl",
    "leviathan": "the_leviathan",
    "whisperer": "the_whisperer",
    "royal_titans": "the_royal_titans",
    "fortis_colosseum": "sol_heredit",
}


def _norm_metric_token(name: str) -> str:
    token = str(name or "").strip().lower()
    for ch in ("'", ":", ",", "."):
        token = token.replace(ch, "")
    for ch in (" ", "-"):
        token = token.replace(ch, "_")
    while "__" in token:
        token = token.replace("__", "_")
    return token.strip("_")


def wom_skill_metric(skill: str) -> Optional[str]:
    """Map a DropTracker skill key ('runecraft') to its WOM skill slug."""
    token = _norm_metric_token(skill)
    token = _SKILL_METRIC_OVERRIDES.get(token, token)
    if _WOM_SKILL_SLUGS and token not in _WOM_SKILL_SLUGS:
        return None
    return token or None


def wom_boss_metric(npc_name: str) -> Optional[str]:
    """Map an NPC display name ('Theatre of Blood: Hard Mode') to its WOM
    boss metric slug, or None when WOM has no hiscores boss metric for it."""
    token = _norm_metric_token(npc_name)
    token = _BOSS_METRIC_OVERRIDES.get(token, token)
    if _WOM_BOSS_SLUGS and token not in _WOM_BOSS_SLUGS:
        return None
    return token or None


def _iso_utc(dt) -> str:
    """Naive-UTC datetime (server tz is UTC) → WOM's ISO-8601 'Z' format."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def _fetch_bulk_route(route, cache_key: str) -> Optional[list]:
    try:
        raw = redis_client.client.get(cache_key)
        if raw is not None:
            return json.loads(raw)
    except Exception as e:
        logger.debug("WOM bulk cache read failed for %s: %s", cache_key, e)
    if not await limiter.wait():
        return None
    await client.start()
    _log_wom_call("bulk.fetch", uri=route.uri)
    try:
        raw = await client._http.fetch(route)
    except Exception as e:
        logger.warning("WOM bulk fetch errored for %s: %s", route.uri, e)
        return None
    if not isinstance(raw, (bytes, bytearray, str)):
        logger.warning("WOM bulk fetch failed for %s: %s", route.uri,
                       getattr(raw, "message", raw))
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning("WOM bulk response unparseable for %s: %s", route.uri, e)
        return None
    if not isinstance(data, list):
        logger.warning("WOM bulk response for %s not a list: %s", route.uri,
                       str(data)[:200])
        return None
    try:
        redis_client.client.setex(cache_key, WOM_BULK_CACHE_TTL, json.dumps(data))
    except Exception as e:
        logger.debug("WOM bulk cache write failed for %s: %s", cache_key, e)
    return data


async def get_group_bulk_gained(wom_group_id: int, start_dt, end_dt) -> Optional[list]:
    """GET /groups/:id/bulk-gained — per-member {metric, gained, start, end}
    for every metric, for members with ≥1 snapshot inside the window. Rows:
    {"player": {...}, "startDate": ..., "endDate": ..., "data": [...]}."""
    route = _BULK_GAINED_ROUTE.compile(int(wom_group_id)).with_params(
        {"startDate": _iso_utc(start_dt), "endDate": _iso_utc(end_dt)})
    cache_key = (f"{_REDIS_BULK_GAINED_PREFIX}{int(wom_group_id)}:"
                 f"{int(start_dt.timestamp())}:{int(end_dt.timestamp())}")
    return await _fetch_bulk_route(route, cache_key)


async def get_group_bulk_hiscores(wom_group_id: int) -> Optional[list]:
    """GET /groups/:id/bulk-hiscores — every member's latest snapshot. Rows:
    {"player": {...}, "data": {"createdAt": ..., "data": {"skills": {...},
    "bosses": {...}, "activities": {...}, "computed": {...}}}}."""
    route = _BULK_HISCORES_ROUTE.compile(int(wom_group_id))
    return await _fetch_bulk_route(
        route, f"{_REDIS_BULK_HISCORES_PREFIX}{int(wom_group_id)}")


async def request_player_update(username: str) -> bool:
    """Ask WOM to re-scrape one player's hiscores (event freshness pass)."""
    if not username or not await limiter.wait():
        return False
    await client.start()
    try:
        _log_wom_call("players.update_player", username=username)
        result = await client.players.update_player(username=username)
        return bool(result.is_ok)
    except Exception as e:
        logger.warning("WOM update_player failed for %s: %s", username, e)
        return False


async def request_group_update_all(wom_group_id: int, verification_code: str) -> Optional[str]:
    """POST /groups/:id/update-all — queue every >24h-outdated member for a
    WOM-side update (WOM paces the queue and retries 3x per player).

    Returns WOM's message on success. On a 400/403 (bad verification code)
    sets ``wom:updateall:badcode:{gid}`` so callers stop retrying the code.
    """
    if not verification_code or not await limiter.wait():
        return None
    await client.start()
    try:
        _log_wom_call("groups.update_outdated_members", id=wom_group_id)
        result = await client.groups.update_outdated_members(
            int(wom_group_id), verification_code)
    except Exception as e:
        logger.warning("WOM update-all errored for group %s: %s", wom_group_id, e)
        return None
    if result.is_ok:
        return getattr(result.unwrap(), "message", "ok")
    err = result.unwrap_err()
    status = getattr(err, "status", -1)
    logger.warning("WOM update-all failed for group %s (status=%s): %s",
                   wom_group_id, status, getattr(err, "message", err))
    if status in (400, 403):
        try:
            redis_client.client.setex(
                f"{UPDATE_ALL_BADCODE_PREFIX}{int(wom_group_id)}",
                7 * 86400, str(status))
        except Exception:
            pass
    return None


async def get_player_all_skills_by_id(wom_id: int):
    """
    Returns all skills and their experience points for a player by WOM ID
    Returns a dictionary with skill names as keys and experience points as values
    """
    if not await limiter.wait():
        return {}
    await client.start()
    _log_wom_call("players.get_details_by_id", player_id=wom_id)
    player_data = await client.players.get_details_by_id(wom_id)

    if player_data.is_ok:
        details = player_data.unwrap()
        snapshot = getattr(details, "latest_snapshot", None)
        
        if snapshot:
            snapshot_data = getattr(snapshot, "data", None)
            if snapshot_data:
                skills = getattr(snapshot_data, "skills", {})
                skills_data = []
                
                for skill_name, skill_obj in skills.items():
                    skill_name_str = str(skill_name).split(".")[-1].lower()
                    skills_data.append({
                        "skill": skill_name_str,
                        "experience": getattr(skill_obj, "experience", 0)
                    })
                
                return skills_data
    
    return {}