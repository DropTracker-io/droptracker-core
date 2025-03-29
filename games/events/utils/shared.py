import logging
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text
from typing import Optional, Union, Dict, Any

from db.eventmodels import EventModel as EventModel, session
from db.models import User, Group

logger = logging.getLogger(__name__)

async def find_group_by_guild_id(guild_id: int) -> Group:
    """Find a group by Discord guild ID"""
    try:
        group = session.query(Group).filter(Group.guild_id == guild_id).first()
        if not group:
            raise ValueError(f"Group with guild_id {guild_id} not found")
        return group
    except SQLAlchemyError as e:
        logger.error(f"Database error in find_group_by_guild_id: {e}")
        raise ValueError("A database error occurred while finding the group")
    
def get_event_by_id(event_id: int, notification_channel_id: Optional[int] = None, 
                  bot=None, force_reload: bool = False):
    """
    Get an event by ID
    
    This is a forwarding function to avoid circular imports.
    The actual implementation is in EventFactory.
    """
    # Lazy import to prevent circular import
    from games.events.EventFactory import get_event_by_id as factory_get_event
    return factory_get_event(event_id, notification_channel_id, bot, force_reload)

def get_event_by_uid(discord_id: str, notification_channel_id: Optional[int] = None, bot=None):
    """
    Get an event by user's Discord ID
    
    This is a forwarding function to avoid circular imports.
    The actual implementation is in EventFactory.
    """
    # Lazy import to prevent circular import
    from games.events.EventFactory import get_event_by_uid as factory_get_uid
    return factory_get_uid(discord_id, notification_channel_id, bot)

def get_or_create_event(event_id: int, notification_channel_id: Optional[int] = None, 
                      bot=None, force_reload: bool = False):
    """
    Get or create an event by ID
    
    This is a forwarding function to avoid circular imports.
    The actual implementation is in EventFactory.
    """
    # Lazy import to prevent circular import
    from games.events.EventFactory import get_or_create_event as factory_get_or_create
    return factory_get_or_create(event_id, notification_channel_id, bot, force_reload)