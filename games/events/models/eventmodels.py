
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

 