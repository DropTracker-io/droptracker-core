from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Union
from db.models import Player

class TileType(Enum):
    AIR = "air"
    WATER = "water"
    EARTH = "earth"
    FIRE = "fire"

@dataclass
class Tile:
    type: TileType
    position: int

class ItemType(Enum):
    OFFENSIVE = "offensive"
    DEFENSIVE = "defensive"
    SPECIAL = "special"

@dataclass
class Item:
    name: str
    cost: int
    effect: str
    emoji: str
    item_type: ItemType
    cooldown: int

@dataclass
class Team:
    name: str
    players: List[Player]
    position: int = 0
    points: int = 0
    inventory: List[Item] = field(default_factory=list)
    cooldowns: Dict[str, int] = field(default_factory=dict)
    active_effects: Dict[str, int] = field(default_factory=dict)

@dataclass
class TaskItem:
    name: str
    points: int = 0

@dataclass
class Task:
    name: str
    difficulty: TileType
    points: int
    required_items: Union[str, List[TaskItem]]
    is_assembly: bool = False