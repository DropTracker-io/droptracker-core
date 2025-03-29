import logging
from typing import Optional, Union, Dict
from sqlalchemy.sql import text

from db.eventmodels import EventModel as EventModel, session
from db.models import User, Group
from games.events.event import Event
from games.events.BoardGame import BoardGame
from games.events.utils.classes.base import EventType

logger = logging.getLogger(__name__)

# Cache to store active event instances
# Key: event_id, Value: event instance
_event_cache: Dict[int, Union[Event, BoardGame]] = {}

def create_new_event(event_type: str, group_id: int, 
                   notification_channel_id: Optional[int] = None, 
                   bot=None) -> Optional[Union[Event, BoardGame]]:
    """
    Create a completely new event of the specified type
    
    Args:
        event_type: Type of event to create
        group_id: ID of the group this event belongs to
        notification_channel_id: ID of the channel to send notifications to
        bot: Discord bot instance
        
    Returns:
        Newly created event object if successful, None otherwise
    """
    try:
        # Create appropriate event type
        event = None
        if event_type == EventType.BOARD_GAME.value:
            event = BoardGame(group_id=group_id, id=-1, notification_channel_id=notification_channel_id, bot=bot)
        # Add more event types here as needed
        elif event_type == EventType.BINGO.value:
            # event = BingoEvent(group_id=group_id, id=-1, notification_channel_id=notification_channel_id, bot=bot)
            logger.warning("Bingo events not yet implemented, creating base event")
            event = Event(group_id=group_id, id=-1, notification_channel_id=notification_channel_id, bot=bot)
        elif event_type == EventType.BOSS_HUNT.value:
            # event = BossHuntEvent(group_id=group_id, id=-1, notification_channel_id=notification_channel_id, bot=bot)
            logger.warning("Boss Hunt events not yet implemented, creating base event")
            event = Event(group_id=group_id, id=-1, notification_channel_id=notification_channel_id, bot=bot)
        else:
            # Default to base event
            logger.warning(f"Unknown event type '{event_type}', creating base Event")
            event = Event(group_id=group_id, id=-1, notification_channel_id=notification_channel_id, bot=bot)
        
        # Add to cache if it has a valid ID
        if event and event.id > 0:
            _event_cache[event.id] = event
            
        return event
            
    except Exception as e:
        logger.error(f"Error creating new event: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def get_or_create_event(event_id: int, notification_channel_id: Optional[int] = None, 
                      bot=None, force_reload: bool = False) -> Optional[Union[Event, BoardGame]]:
    """
    Get an existing event by ID, or create an instance if it doesn't exist in the cache
    
    Args:
        event_id: ID of the event
        notification_channel_id: ID of the channel to send notifications to
        bot: Discord bot instance
        force_reload: Whether to force reload from database even if cached
        
    Returns:
        Event object if found/created, None otherwise
    """
    try:
        # Check if we already have this event in the cache and we're not forcing a reload
        if not force_reload and event_id in _event_cache:
            logger.debug(f"Returning cached event instance for event ID {event_id}")
            # Update the notification channel and bot if provided
            if notification_channel_id is not None:
                _event_cache[event_id].notification_channel_id = notification_channel_id
            if bot is not None:
                _event_cache[event_id].bot = bot
                
            return _event_cache[event_id]
        
        # If we need to load from database
        event_model = session.query(EventModel).filter(EventModel.id == event_id).first()
        if not event_model:
            logger.error(f"Event {event_id} not found in database")
            return None
        
        # Create appropriate event type
        event = None
        if event_model.type == EventType.BOARD_GAME.value or event_model.type == "board_game":
            event = BoardGame(group_id=event_model.group_id, id=event_id, 
                            notification_channel_id=notification_channel_id, bot=bot)
        # Add more event types here as needed
        elif event_model.type == EventType.BINGO.value:
            # event = BingoEvent(...)
            logger.warning("Bingo events not yet implemented, returning base event")
            event = Event(group_id=event_model.group_id, id=event_id, 
                        notification_channel_id=notification_channel_id, bot=bot)
        elif event_model.type == EventType.BOSS_HUNT.value:
            # event = BossHuntEvent(...)
            logger.warning("Boss Hunt events not yet implemented, returning base event")
            event = Event(group_id=event_model.group_id, id=event_id, 
                        notification_channel_id=notification_channel_id, bot=bot)
        else:
            # Default to base event
            logger.warning(f"Unknown event type '{event_model.type}', using base Event")
            event = Event(group_id=event_model.group_id, id=event_id, 
                        notification_channel_id=notification_channel_id, bot=bot)
        
        # Add to cache
        if event:
            _event_cache[event_id] = event
            
        return event
            
    except Exception as e:
        logger.error(f"Error getting event: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def get_event_by_id(event_id: int, notification_channel_id: Optional[int] = None, 
                  bot=None, force_reload: bool = False) -> Optional[Union[Event, BoardGame]]:
    """
    Get an event by ID
    
    Args:
        event_id: ID of the event
        notification_channel_id: ID of the channel to send notifications to
        bot: Discord bot instance
        force_reload: Whether to force reload from database
        
    Returns:
        Event object if found, None otherwise
    """
    return get_or_create_event(event_id, notification_channel_id, bot, force_reload)

def get_event_by_uid(discord_id: str, notification_channel_id: Optional[int] = None, 
                   bot=None) -> Optional[Union[Event, BoardGame]]:
    """
    Get the event associated with a user's Discord ID
    
    Args:
        discord_id: Discord ID of the user
        notification_channel_id: ID of the channel to send notifications to
        bot: Discord bot instance
        
    Returns:
        Event object if found, None otherwise
    """
    try:
        # Get user
        user = session.query(User).filter(User.discord_id == str(discord_id)).first()
        if not user:
            logger.warning(f"User with Discord ID {discord_id} not found")
            return None
        
        # Get user's groups
        stmt = text("SELECT group_id FROM user_group_association WHERE user_id = :user_id")
        groups = session.execute(stmt, {"user_id": user.user_id}).fetchall()
        logger.debug(f"Found {len(groups)} groups for user: {user.user_id}")
        
        for group in groups:
            group_id = group[0]
            group_obj = session.query(Group).filter(Group.group_id == group_id).first()
            
            if not group_obj:
                continue
                
            # Find active events for this group
            events = session.query(EventModel).filter(
                EventModel.group_id == group_obj.group_id,
                EventModel.status == "active"
            ).all()
            
            # Return the first active event found
            if events:
                event = events[0]  # Get the first active event
                logger.info(f"Found active event {event.id} for user {discord_id} in group {group_id}")
                
                # Get the appropriate event object
                return get_or_create_event(
                    event_id=event.id, 
                    notification_channel_id=notification_channel_id, 
                    bot=bot
                )
            else:
                logger.debug(f"No active event found for user in group: {group_id}")
        
        # If we get here, no active event was found
        logger.info(f"No active events found for user {discord_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting event by user ID: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

def clear_event_cache() -> None:
    """
    Clear the event cache
    
    This can be useful for testing or when you want to force reload of all events.
    """
    global _event_cache
    _event_cache = {}
    logger.info("Event cache cleared")

def remove_event_from_cache(event_id: int) -> bool:
    """
    Remove an event from the cache
    
    Args:
        event_id: ID of the event to remove
        
    Returns:
        True if event was in cache and removed, False otherwise
    """
    if event_id in _event_cache:
        del _event_cache[event_id]
        logger.debug(f"Removed event {event_id} from cache")
        return True
    return False 