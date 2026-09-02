"""
Main API client that coordinates all OSRS API interactions.
"""

import aiohttp
from typing import Optional
from utils.wiki_ua import USER_AGENT
from .semantic import SemanticAPI
from .pricing import PricingAPI


# Re-exported for the existing callers; the string itself lives in
# utils/wiki_ua.py so there is exactly one identity to keep off a blocklist.
# A second private copy of it here is what left utils/ge_value.py 403ing for
# five days after the 2026-08-20 block was "fixed" — see that module.
DEFAULT_USER_AGENT = USER_AGENT


class OSRSAPIClient:
    """
    Main client for managing HTTP sessions and providing access to OSRS wiki's Bucket API + GE prices
    """

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, cache=None):
        """
        Initialize the OSRS API client.

        Args:
            user_agent: User agent string for API requests
            cache: Optional redis-like object (``get(key)``/``set(key, value,
                ex=seconds)``) used by SemanticAPI to cache drop-source
                lookups. None disables caching.
        """
        self.user_agent = user_agent
        self.cache = cache
        self._wiki_session: Optional[aiohttp.ClientSession] = None
        self._prices_session: Optional[aiohttp.ClientSession] = None

        # Initialize sub-APIs
        self.semantic = SemanticAPI(self)
        self.pricing = PricingAPI(self)
    
    async def get_wiki_session(self) -> aiohttp.ClientSession:
        """Get or create the wiki API session."""
        if self._wiki_session is None or self._wiki_session.closed:
            self._wiki_session = aiohttp.ClientSession(
                headers={'User-Agent': self.user_agent}
            )
        return self._wiki_session
    
    async def get_prices_session(self) -> aiohttp.ClientSession:
        """Get or create the prices API session."""
        if self._prices_session is None or self._prices_session.closed:
            self._prices_session = aiohttp.ClientSession(
                headers={'User-Agent': self.user_agent}
            )
        return self._prices_session
    
    async def close(self):
        """Close all HTTP sessions."""
        if self._wiki_session and not self._wiki_session.closed:
            await self._wiki_session.close()
        if self._prices_session and not self._prices_session.closed:
            await self._prices_session.close()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
