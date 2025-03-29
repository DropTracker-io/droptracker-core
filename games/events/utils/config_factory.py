import logging
from typing import Optional, Union

from db.eventmodels import EventModel as EventModel, session
from games.events.utils.event_config import EventConfig
from games.events.utils.bg_config import BoardGameConfig
from games.events.utils.classes.base import EventType

logger = logging.getLogger(__name__)

def get_config_for_event(event_id: int) -> Optional[Union[EventConfig, BoardGameConfig]]:
    """
    Get the appropriate configuration object for an event
    
    Args:
        event_id: ID of the event
        
    Returns:
        Configuration object for the event, or None if event not found
    """
    try:
        # Get event from database
        event = session.query(EventModel).filter(EventModel.id == event_id).first()
        if not event:
            logger.error(f"Event {event_id} not found in database")
            return None
        
        # Determine event type
        event_type = event.type
        
        # Create appropriate configuration object
        if event_type == EventType.BOARD_GAME.value or event_type == "board_game":
            return BoardGameConfig(event_id)
        # Add more event types here as needed
        else:
            # Default to base event config
            logger.warning(f"Unknown event type '{event_type}', using base EventConfig")
            return EventConfig(event_id)
            
    except Exception as e:
        logger.error(f"Error getting configuration for event {event_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None