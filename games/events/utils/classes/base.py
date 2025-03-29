from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from db.eventmodels import EventParticipant, EventTeamInventory, EventTeamModel, session

class TileType(Enum):
    AIR = "air"
    WATER = "water"
    EARTH = "earth"
    FIRE = "fire"

@dataclass
class Tile:
    """Represents a tile on the board"""
    type: TileType  # The type of tile (AIR, WATER, EARTH, FIRE)
    position: int   # The position on the board


@dataclass
class TaskItem:
    name: str
    points: int


@dataclass
class Task:
    """Represents a task in the board game"""
    name: str
    description: str
    difficulty: TileType
    required_items: List[TaskItem]
    points: int = 0 #for point collection tasks
    is_assembly: bool = False
    task_id: Optional[int] = None
    type: str = "exact_item"  # Can be "exact_item", "assembly", "point_collection", or "any_of"



@dataclass
class Team:
    """Represents a team in the board game"""
    name: str
    position: int = 0
    points: int = 0
    team_id: Optional[int] = None
    current_task: Optional[Task] = None
    current_task_id: Optional[int] = None  # Add this field to store the task ID
    task_progress: int = 0
    gold: int = 0
    cooldowns: Dict[str, int] = field(default_factory=dict)
    active_effects: Dict[str, int] = field(default_factory=dict)
    mercy_rule: Optional[datetime] = None
    mercy_count: int = 0
    assembled_items: Optional[str] = None
    turn_number: int = 1

    def _get_inventory(self) -> List[EventTeamInventory]:
        """Get the inventory of the team"""
        team = session.query(EventTeamModel).filter(EventTeamModel.id == self.team_id).first()
        return [item for item in team.inventory]

    def _get_players(self) -> List[EventParticipant]:
        """Get the players in the team"""
        participants = session.query(EventParticipant).filter(EventParticipant.team_id == self.team_id).all()
        return [participant.player for participant in participants]

class EventType(Enum):
    BOARD_GAME = "BoardGame"
    BINGO = "Bingo"
    BOSS_HUNT = "BossHunt"


class ShopItemType(Enum):
    OFFENSIVE = "offensive"
    DEFENSIVE = "defensive"
    SPECIAL = "special"


@dataclass
class ShopItem:
    name: str
    cost: int
    effect: str
    emoji: str
    item_type: ShopItemType
    cooldown: int
