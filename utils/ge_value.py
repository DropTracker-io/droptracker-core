"""Server-side Grand Exchange valuation for incoming drops.

``get_true_item_value`` is the single authority on what a drop is worth: an
``item_value_overrides`` rule if one matches, else the live GE price, else the
(spoofable) client-reported value as a last resort.

**Everything else in this module exists to keep our request volume off the
wiki's radar.** Two blocklistings taught us why:

* The identity now comes from :mod:`utils.wiki_ua` — this module used to keep
  a private copy of the User-Agent, so when the 2026-08-20 block was fixed in
  ``osrs_api/client.py`` this copy stayed blocklisted. On 2026-08-28
  ``prices.runescape.wiki`` started 403ing it and every price lookup here
  returned ``None`` in silence for five days.
* The volume that earns a blocklist came from the *failure* path. Prices were
  cached only on success, so every unpriceable item — untradeables, junk, the
  ~60k zero-value drops a day — missed the cache forever, and its name
  fallback re-downloaded the entire ~860KB ``/mapping`` document. That is on
  the order of 50GB/day pulled from a volunteer-run API to learn, over and
  over, that bones are not tradeable.

So: the mapping is cached, misses are cached, override components go through
the same cache as everything else, and a circuit breaker stops us hammering an
endpoint that is already refusing us. Non-200s are logged at ERROR (throttled)
rather than swallowed, because the whole cost of the 2026-08-28 outage was that
nobody could see it.
"""
import asyncio
import json
import logging
import time

import aiohttp

from utils.redis import RedisClient
from utils.wiki_ua import USER_AGENT
from utils import value_overrides

logger = logging.getLogger("app.ge_value")

# Base URLs for the APIs
PRICES_API_BASE = "https://prices.runescape.wiki/api/v1/osrs"
WIKI_API_BASE = "https://oldschool.runescape.wiki/api.php"

_redis_client = RedisClient()

ITEM_PRICE_CACHE_TTL = 3600  # a resolved price (1 hour)
# "The GE has no price for this item" is a stable fact — untradeables do not
# become tradeable — and caching it is what takes the /mapping stampede away.
ITEM_PRICE_MISS_CACHE_TTL = 21600  # 6 hours
MAPPING_CACHE_TTL = 86400  # the id/name mapping changes on game updates only
MAPPING_MICRO_TTL = 300.0  # in-process, keeps the 860KB blob out of Redis round-trips

# No request timeout at all used to be the norm here; a hanging wiki call in
# the drop path has already cost us a SIGTERM restart loop once (see
# data/submissions/drop.py).
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Circuit breaker. When the API is refusing us there is nothing to gain from
# asking again per drop — at ~670k drops/day that is the behaviour that turns
# a block into a reason to keep the block.
BREAKER_TRIP_AFTER = 10  # consecutive transport failures
BREAKER_COOLDOWN = 120.0  # seconds to stop calling before probing again

# Create a single aiohttp session for reuse
prices_session = None
wiki_session = None


class PriceApiUnavailable(RuntimeError):
    """The prices API could not be reached, or refused us.

    Deliberately distinct from "this item has no GE price": that is a real
    answer and gets cached, while this one must not be, or a short outage
    would freeze zeros in for ``ITEM_PRICE_MISS_CACHE_TTL``.
    """


async def get_prices_session():
    global prices_session
    if prices_session is None or prices_session.closed:
        prices_session = aiohttp.ClientSession(
            headers={'User-Agent': USER_AGENT}, timeout=REQUEST_TIMEOUT
        )
    return prices_session

async def get_wiki_session():
    global wiki_session
    if wiki_session is None or wiki_session.closed:
        wiki_session = aiohttp.ClientSession(
            headers={'User-Agent': USER_AGENT}, timeout=REQUEST_TIMEOUT
        )
    return wiki_session


# --------------------------------------------------------------------------- #
# Failure visibility + circuit breaker
# --------------------------------------------------------------------------- #
_LOG_THROTTLE = 300.0  # seconds between ERROR lines per endpoint
_last_logged: dict = {}
_consecutive_failures = 0
_breaker_open_until = 0.0


def _log_transport_failure(endpoint: str, detail: str) -> None:
    """Report a price-API failure at ERROR level, at most once per endpoint per
    ``_LOG_THROTTLE``.

    ERROR routes to Sentry via the logging integration (see db/app_logger.py),
    which is the alert this module lacked entirely: on 2026-08-28 every call
    started returning 403 and the only visible symptom was drops quietly
    valued at 0. Throttled because the alternative is a log line per drop.
    """
    now = time.monotonic()
    if now - _last_logged.get(endpoint, 0.0) >= _LOG_THROTTLE:
        _last_logged[endpoint] = now
        logger.error(
            "GE price API unavailable (%s): %s — drops are falling back to "
            "client-reported values and override-priced items to their "
            "fallback_value. Check the User-Agent in utils/wiki_ua.py against "
            "a manual request before assuming a transient outage.",
            endpoint,
            detail,
        )
    # Breadcrumb for /admin/status and for whoever investigates next.
    try:
        _redis_client.setex(
            "ge_price_api:last_failure",
            86400,
            json.dumps({"endpoint": endpoint, "detail": detail, "at": int(time.time())}),
        )
    except Exception:
        pass


def _breaker_is_open() -> bool:
    return time.monotonic() < _breaker_open_until


def _record_failure() -> None:
    global _consecutive_failures, _breaker_open_until
    _consecutive_failures += 1
    if _consecutive_failures >= BREAKER_TRIP_AFTER:
        _breaker_open_until = time.monotonic() + BREAKER_COOLDOWN


def _record_success() -> None:
    global _consecutive_failures, _breaker_open_until
    _consecutive_failures = 0
    _breaker_open_until = 0.0


# --------------------------------------------------------------------------- #
# Cached price lookups
# --------------------------------------------------------------------------- #
async def _lookup_and_cache_ge_price(item_name: str, item_id=None) -> int:
    """Cached GE price for an item. Returns 0 when it has none (or we can't ask).

    Both outcomes cache: a price for an hour, a confirmed miss for six. Only a
    transport failure is left uncached, so an outage can't be mistaken for a
    permanent "worthless".
    """
    cache_key = f"item_price:{item_id}" if item_id else f"item_price_name:{item_name.lower()}"
    cached = _redis_client.get(cache_key)
    if cached is not None:
        try:
            return int(cached)  # a cached "0" is a cached miss
        except (ValueError, TypeError):
            pass

    try:
        price = None
        if item_id:
            price = await _price_by_id(item_id)
        if not price and item_name:
            price = await _price_by_name(item_name)
    except PriceApiUnavailable:
        return 0

    if price:
        _redis_client.setex(cache_key, ITEM_PRICE_CACHE_TTL, str(price))
        return int(price)

    # The API answered and has no price for this item. Caching this is the
    # whole point: it is the path that used to re-download /mapping per drop.
    _redis_client.setex(cache_key, ITEM_PRICE_MISS_CACHE_TTL, "0")
    return 0


async def build_component_price_map(overrides) -> dict:
    """Fetch each distinct component price once, in parallel, for a batch of
    overrides. Used by the admin preview and the public /item-values listing so
    they don't hit the GE API once per (override × component). The result is
    keyed by ``value_overrides.component_price_key`` to match
    ``compute_override_from_prices``.

    Goes through :func:`_lookup_and_cache_ge_price` rather than calling the API
    directly — this path used to bypass the cache entirely, so every vestige or
    Araxxor-part drop re-fetched its component live.
    """
    wanted: dict = {}
    for override in overrides:
        for component in override.get("components") or []:
            wanted.setdefault(value_overrides.component_price_key(component), component)

    async def _fetch(component):
        price = await _lookup_and_cache_ge_price(
            component.get("item_name") or "", component.get("item_id")
        )
        # None, not 0: compute_override_from_prices reads a falsy price as
        # "unpriced" and applies the rule's fallback_value.
        return price or None

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
        # value the client reported when no fallback is configured. For an
        # untradeable component that client value is 0 — which is exactly how
        # the 2026-08-28 403 turned into Araxxor parts worth nothing.
        fallback = override.get("fallback_value") or 0
        return fallback if fallback else provided_value

    # The client-supplied value is spoofable, so prefer the server GE price for
    # any priceable item; fall back to the client value only when unpriceable.
    server_price = await _lookup_and_cache_ge_price(item_name, item_id)
    if server_price:
        return server_price
    return provided_value


# --------------------------------------------------------------------------- #
# Item id/name mapping (cached — see the module docstring)
# --------------------------------------------------------------------------- #
_MAPPING_REDIS_KEY = "ge:item_mapping"
_mapping_micro = None
_mapping_micro_expires = 0.0
_name_index = None


async def _fetch_mapping():
    """Download the mapping document. Raises :class:`PriceApiUnavailable`."""
    if _breaker_is_open():
        raise PriceApiUnavailable("circuit breaker open")
    endpoint = f"{PRICES_API_BASE}/mapping"
    session = await get_prices_session()
    try:
        async with session.get(endpoint) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                _record_failure()
                _log_transport_failure("mapping", f"HTTP {resp.status} — {body!r}")
                raise PriceApiUnavailable(f"mapping -> HTTP {resp.status}")
            data = await resp.json()
    except PriceApiUnavailable:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        _record_failure()
        _log_transport_failure("mapping", f"{type(exc).__name__}: {exc}")
        raise PriceApiUnavailable(str(exc)) from exc
    _record_success()
    return data


async def get_mapping():
    """Fetch the item mapping data which contains names, IDs, and other metadata.

    Cached in Redis for a day, with a short in-process cache on top. Uncached,
    this ~860KB document was pulled once per unpriceable drop.

    Returns ``None`` when it cannot be fetched and nothing is cached — callers
    already treat that as "no mapping available".
    """
    global _mapping_micro, _mapping_micro_expires, _name_index
    now = time.monotonic()
    if _mapping_micro is not None and now < _mapping_micro_expires:
        return _mapping_micro

    mapping = None
    cached = _redis_client.get(_MAPPING_REDIS_KEY)
    if cached:
        try:
            mapping = json.loads(cached)
        except (ValueError, TypeError):
            mapping = None

    if mapping is None:
        try:
            mapping = await _fetch_mapping()
        except PriceApiUnavailable:
            return None
        if mapping:
            try:
                _redis_client.setex(
                    _MAPPING_REDIS_KEY, MAPPING_CACHE_TTL, json.dumps(mapping)
                )
            except Exception:
                pass

    if mapping:
        _mapping_micro = mapping
        _mapping_micro_expires = time.monotonic() + MAPPING_MICRO_TTL
        _name_index = None  # rebuilt lazily against the refreshed mapping
    return mapping


async def find_item_id_by_name(name):
    """Find an item ID by name using the mapping data"""
    mapping_data = await get_mapping()
    if not mapping_data:
        return None

    global _name_index
    if _name_index is None:
        index = {}
        for item in mapping_data:
            item_name = (item.get('name') or '').strip().lower()
            if item_name and item.get('id') is not None:
                index.setdefault(item_name, item['id'])
        _name_index = index

    return _name_index.get(name.strip().lower())


# --------------------------------------------------------------------------- #
# Live price endpoints
# --------------------------------------------------------------------------- #
async def _fetch_latest_price_data(item_id):
    """Latest prices for one item.

    Raises :class:`PriceApiUnavailable` when the API refused or could not be
    reached; returns ``None`` only when it answered and holds no data for this
    item. That distinction is what keeps an outage out of the miss cache.
    """
    if _breaker_is_open():
        raise PriceApiUnavailable("circuit breaker open")
    endpoint = f"{PRICES_API_BASE}/latest"
    session = await get_prices_session()
    try:
        async with session.get(endpoint, params={'id': item_id}) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                _record_failure()
                _log_transport_failure("latest", f"HTTP {resp.status} — {body!r}")
                raise PriceApiUnavailable(f"latest -> HTTP {resp.status}")
            data = await resp.json()
    except PriceApiUnavailable:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        _record_failure()
        _log_transport_failure("latest", f"{type(exc).__name__}: {exc}")
        raise PriceApiUnavailable(str(exc)) from exc

    _record_success()
    if 'data' not in data:
        return None
    return data['data'].get(str(item_id)) or None


async def _price_by_id(item_id):
    """Most recent price by id. Raises :class:`PriceApiUnavailable`."""
    if not item_id:
        return None

    price_data = await _fetch_latest_price_data(item_id)
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


async def _price_by_name(item_name):
    """Most recent price by name. Raises :class:`PriceApiUnavailable`."""
    item_id = await find_item_id_by_name(item_name)
    if not item_id:
        return None
    return await _price_by_id(item_id)


async def get_latest_price_data(item_id):
    """Fetch the latest price data from the real-time prices API.

    Returns ``None`` on any failure — kept for callers that only want a price
    or nothing. Inside this module use :func:`_fetch_latest_price_data`, which
    distinguishes "unavailable" from "no such price".
    """
    try:
        return await _fetch_latest_price_data(item_id)
    except PriceApiUnavailable:
        return None


async def get_most_recent_price_by_id(item_id):
    """
    Get the most recent price for an item by ID
    Returns the price as an integer, or None if not found
    """
    try:
        return await _price_by_id(item_id)
    except PriceApiUnavailable:
        return None


async def get_most_recent_price_by_name(item_name):
    """
    Get the most recent price for an item by name
    Returns the price as an integer, or None if not found
    """
    try:
        return await _price_by_name(item_name)
    except PriceApiUnavailable:
        return None

async def close_aiohttp_sessions():
    global prices_session, wiki_session
    if prices_session is not None and not prices_session.closed:
        await prices_session.close()
    if wiki_session is not None and not wiki_session.closed:
        await wiki_session.close()
