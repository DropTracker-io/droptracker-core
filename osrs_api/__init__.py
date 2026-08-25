"""
OSRS API Package

A unified package for the DropTracker's interactions with Old School RuneScape Wiki's APIs including:
- OSRS Wiki Bucket API (for item/monster data and drop verification/information)
- RuneScape Wiki Prices API (for Grand Exchange pricing)

This package provides a clean interface for common operations like:
- Checking if an item drops from a specific NPC
- Getting item and NPC IDs
- Retrieving Grand Exchange prices
- Getting combat achievement tier information
"""

from .client import OSRSAPIClient, DEFAULT_USER_AGENT
from .semantic import SemanticAPI
from .pricing import PricingAPI

__version__ = "1.0.0"
__all__ = ["OSRSAPIClient", "SemanticAPI", "PricingAPI", "DEFAULT_USER_AGENT"]

# Convenience function to create a fully configured client
def create_client(user_agent: str = DEFAULT_USER_AGENT, cache=None) -> OSRSAPIClient:
    """
    Create a fully configured OSRS API client with all sub-APIs initialized.

    Args:
        user_agent: User agent string for API requests
        cache: Optional redis-like cache for drop-source lookups (see
            OSRSAPIClient)

    Returns:
        Configured OSRSAPIClient instance
    """
    return OSRSAPIClient(user_agent, cache=cache)
