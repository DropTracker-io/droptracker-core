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
- EventTask: Event tasks (in tasks/)
- AssignedTask: Tasks assigned to teams (in tasks/)
- BoardGameModel: Board game specific event data (in types/)
- BingoBoardModel: Bingo board model (in types/bingo/)
- BingoBoardTile: Bingo board tiles (in types/bingo/)
"""

# Import all core models
from .EventModel import EventModel
from .EventConfigModel import EventConfigModel
from .EventTeamModel import EventTeamModel
from .EventParticipant import EventParticipant
from .EventShopItem import EventShopItem
from .EventTeamInventory import EventTeamInventory
from .EventTeamCooldown import EventTeamCooldown
from .EventTeamEffect import EventTeamEffect

# Import task models from tasks submodule
from .tasks import EventTask, AssignedTask, BaseTask, TrackedTaskData, TaskType

# Import event type models
from .types.BoardGame import BoardGameModel
from .types.bingo import BingoBoardModel, BingoBoardTile, BingoGameModel

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
    'TrackedTaskData',
    'BaseTask',
    'BoardGameModel',
    'BingoBoardModel',
    'BingoBoardTile',
    'BingoGameModel'
] 

