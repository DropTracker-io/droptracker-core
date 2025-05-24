# Event models package
"""
Event models for the droptracker application.

This package contains all event-related database models:
- EventModel: Base event model
- EventConfigModel: Event configuration storage
- EventTeamModel: Event teams
- EventParticipant: Event participants
- EventShopItem: Items available in events
- EventTeamInventory: Team inventory management
- EventTeamCooldown: Team cooldowns
- EventTeamEffect: Team effects
- EventTask: Event tasks
- AssignedTask: Tasks assigned to teams
- BoardGameModel: Board game specific event data (in types/)
"""

# Import all models
from .EventModel import EventModel
from .EventConfigModel import EventConfigModel
from .EventTeamModel import EventTeamModel
from .EventParticipant import EventParticipant
from .EventShopItem import EventShopItem
from .EventTeamInventory import EventTeamInventory
from .EventTeamCooldown import EventTeamCooldown
from .EventTeamEffect import EventTeamEffect
from .EventTask import EventTask
from .AssignedTask import AssignedTask

# Import event type models
from .types.BoardGame import BoardGameModel

# Export all models
__all__ = [
    'EventModel',
    'EventConfigModel',
    'EventTeamModel',
    'EventParticipant',
    'EventShopItem',
    'EventTeamInventory',
    'EventTeamCooldown',
    'EventTeamEffect',
    'EventTask',
    'AssignedTask',
    'BoardGameModel',
] 