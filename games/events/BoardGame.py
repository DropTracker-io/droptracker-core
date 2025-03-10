from collections import Counter
import json
import random
import time
import logging
from typing import Dict, Any, Optional, List, Tuple, Union
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
import traceback

import interactions
from sqlalchemy.orm.exc import NoResultFound
from db.base import session
from db.eventmodels import Event as EventModel, EventConfig, EventTask, EventTeam, EventParticipant, EventItems, EventTeamInventory, EventTeamCooldown, EventTeamEffect
from db.models import Player, User, Group
from utils.logger import LoggerClient
import os

from dotenv import load_dotenv
from games.events.utils.bg_config import BoardGameConfig

default_tasks_raw = json.load(open("games/events/task_store/default.json"))
default_tasks = default_tasks_raw["tasks"]

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("events.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("events")

class TileType(Enum):
    AIR = "air"
    WATER = "water"
    EARTH = "earth"
    FIRE = "fire"

AIR_EMOJI = "<:air_rune:1348351869403136040>"
WATER_EMOJI = "<:water_rune:1348351872557387827>"
EARTH_EMOJI = "<:earth_rune:1348351870334144644>"
FIRE_EMOJI = "<:fire_rune:1348351871512743966>"

    

@dataclass
class Tile:
    """Represents a tile on the board"""
    type: TileType  # The type of tile (AIR, WATER, EARTH, FIRE)
    position: int   # The position on the board

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
        team = session.query(EventTeam).filter(EventTeam.id == self.team_id).first()
        return [item for item in team.inventory]

    def _get_players(self) -> List[EventParticipant]:
        """Get the players in the team"""
        participants = session.query(EventParticipant).filter(EventParticipant.team_id == self.team_id).all()
        return [participant.player for participant in participants]

class BoardGameState:
    """Class to handle saving and loading game state"""
    
    @staticmethod
    def save_state(game: 'BoardGame') -> int:
        """
        Save the current game state to the database
        
        Args:
            game: The BoardGame instance
            
        Returns:
            update_number: The version number of this state update
        """
        # Get the current event
        event: EventModel = session.query(EventModel).get(game.event_id)
        if not event:
            raise ValueError(f"Event with ID {game.event_id} not found")
            
        # Increment update number
        update_number = event.update_number + 1
        event.update_number = update_number
        
        # Serialize game state
        game_state = game.serialize()
        
        # Save as event configuration
        config = EventConfig(
            event_id=game.event_id,
            config_key="game_state",
            config_value=f"state_{update_number}",
            long_value=json.dumps(game_state),
            update_number=update_number
        )
        
        session.add(config)
        session.commit()
        
        return update_number
    
    @staticmethod
    def load_state(event_id: int, version: Optional[int] = None) -> Dict[str, Any]:
        """
        Load game state from the database
        
        Args:
            event_id: The ID of the event
            version: Optional specific version to load, defaults to latest
            
        Returns:
            The game state as a dictionary
        """
        if version is None:
            # Get latest version
            event = session.query(EventModel).get(event_id)
            if not event:
                raise ValueError(f"Event with ID {event_id} not found")
            version = event.update_number
        
        # Get the state configuration
        config = session.query(EventConfig).filter(
            EventConfig.event_id == event_id,
            EventConfig.update_number == version,
            EventConfig.config_key == "game_state"
        ).first()
        
        if not config:
            raise ValueError(f"Game state version {version} not found for event {event_id}")
            
        return json.loads(config.long_value)
    
    @staticmethod
    def list_versions(event_id: int) -> List[Dict[str, Any]]:
        """
        List all available versions of game state for an event
        
        Args:
            event_id: The ID of the event
            
        Returns:
            List of dictionaries with version info
        """
        configs = session.query(EventConfig).filter(
            EventConfig.event_id == event_id,
            EventConfig.config_key == "game_state"
        ).order_by(EventConfig.update_number).all()
        
        return [
            {
                "version": config.update_number,
                "timestamp": config.updated_at.isoformat(),
                "key": config.config_value
            }
            for config in configs
        ]

class BoardGame:
    """Board game implementation for events"""
    
    def __init__(self, event_id: int, notification_channel_id: int = None, bot: interactions.Client = None):
        """
        Initialize the board game
        
        Args:
            event_id: ID of the event
            notification_channel_id: Channel ID for notifications
            bot: Discord bot instance for sending messages
        """
        self.event_id = event_id
        self.notification_channel_id = notification_channel_id
        self.bot = bot
        self.tiles = []
        self.teams = []
        self.tasks = []
        self.shop_items = []
        self.current_turn = 0
        self.event_status = None
        
        # Load configuration
        self.config = BoardGameConfig(event_id)
        
        # Load event status from database
        self._load_event_status()
        

    def _load_event_status(self):
        """Load the event status from the database"""
        try:
            event = session.get(EventModel, self.event_id)
            if event:
                self.event_status = event.status
                logger.info(f"Loaded event status: {self.event_status} for event {self.event_id}")
            else:
                logger.error(f"Event {self.event_id} not found in database")
                self.event_status = "unknown"
        except Exception as e:
            logger.error(f"Error loading event status: {e}")
            self.event_status = "unknown"

    def _roll_dice(self, dice_count: int, dice_sides: int) -> int:
        """
        Rolls the configured number of dice, with the configured number of sides.
        """
        if dice_count > 1:
            results = [random.randint(1, dice_sides) for _ in range(dice_count)]
            return results, sum(results)
        else:
            roll = random.randint(1, dice_sides)
            return [roll], roll
    
    def _check_event_active(self) -> bool:
        """
        Check if the event is active
        
        Returns:
            True if event is active, False otherwise
        """
        # Refresh event status
        self._load_event_status()
        return self.event_status == "active"
    
    def _generate_tiles(self, board_size: int) -> List[Tile]:
        """
        Generate a fixed board with 142 tiles in a repeating pattern of air, water, earth, fire.
        
        Returns:
            A list of Tile objects representing the game board
        """
        tiles = []
        tile_types = [TileType.AIR, TileType.WATER, TileType.EARTH, TileType.FIRE]
        
        # Create 142 tiles in a repeating pattern
        for i in range(142):
            tile_type = tile_types[i % 4]  # Cycle through the 4 tile types
            tiles.append(Tile(type=tile_type, position=i))
        self.tiles = tiles
    
    def _load_shop_items(self) -> List[Item]:
        """
        Load shop items from the database
        
        Returns:
            List of Item objects available in the shop
        """
        db_items = session.query(EventItems).filter(
            EventItems.event_id == self.event_id
        ).all()
        
        items = []
        for db_item in db_items:
            item = Item(
                name=db_item.name,
                cost=db_item.quantity,  # Using quantity as cost
                effect=db_item.description or "",
                emoji="🔮",  # Default emoji
                item_type=ItemType(db_item.type),
                cooldown=db_item.cooldown
            )
            items.append(item)
        
        return items
    
    def _load_tasks(self) -> List[Task]:
        """
        Load tasks from the database or a JSON file
        
        Returns:
            List of Task objects
        """
        # Try to load tasks from EventConfig
        print("Loading tasks")
        config = session.query(EventConfig).filter(
            EventConfig.event_id == self.event_id,
            EventConfig.config_key == "tasks"
        ).first()
        
        if config and config.long_value:
            tasks_data = json.loads(config.long_value)
            self.tasks = self._parse_tasks(tasks_data)
            return self.tasks
        print("No configured tasks found, loading default tasks")
        # If no tasks in database, load from default file
        try:
            with open("games/events/task_store/default.json", "r") as f:
                tasks_data = json.load(f)
                #print("Loaded tasks data: ", tasks_data)
                tasks = tasks_data["tasks"]
                parsed_tasks = self._parse_tasks(tasks)
                self.tasks = parsed_tasks
                print(f"Loaded {len(parsed_tasks)} tasks")
        except Exception as e:
            print(f"Error loading tasks: {e}")
            return []

    def get_tile_emoji(self, rune=None, tile_type=None, tile_num=0):
        if tile_num != 0:
            print("Tile number is not 0: ", tile_num)
            tile_num = int(tile_num)
        """
        Get the emoji for a tile type
        
        Args:
            rune: Rune type (air, water, earth, fire)
            tile_type: Tile type enum or string representation
            tile_num: Tile number
            
        Returns:
            Emoji string
        """
        # Handle string representation of TileType enum
        if isinstance(tile_type, str):
            if tile_type.startswith('TileType.'):
                # Extract the enum value from the string (e.g., 'TileType.EARTH' -> 'EARTH')
                enum_value = tile_type.split('.')[1] if '.' in tile_type else tile_type
                # Convert to lowercase to match rune values
                rune = enum_value.lower()
            else:
                # If it's just a string like 'earth', use it directly
                rune = tile_type.lower()
        elif tile_type:
            # If it's an actual TileType enum
            if tile_type == TileType.AIR:
                rune = "air"
            elif tile_type == TileType.WATER:
                rune = "water"
            elif tile_type == TileType.EARTH:
                rune = "earth"
            elif tile_type == TileType.FIRE:
                rune = "fire"
        elif tile_num and tile_num > 0:
            # Check if we have tiles loaded in the instance
            if hasattr(self, 'tiles') and self.tiles:
                # If the tile exists in our tiles dictionary
                if tile_num in self.tiles:
                    tile = self.tiles[tile_num]
                    if hasattr(tile, 'type'):
                        # Get the type value
                        if hasattr(tile.type, 'value'):
                            rune = tile.type.value
                        else:
                            rune = tile.type
                else:
                    # If tile doesn't exist, calculate its type based on position
                    tile_types = [TileType.AIR, TileType.WATER, TileType.EARTH, TileType.FIRE]
                    tile_type = tile_types[(tile_num - 1) % 4]  # Cycle through the 4 tile types (1-indexed)
                    rune = tile_type.value
            else:
                # If no tiles are loaded, calculate type based on position
                tile_types = [TileType.AIR, TileType.WATER, TileType.EARTH, TileType.FIRE]
                tile_type = tile_types[(tile_num - 1) % 4]  # Cycle through the 4 tile types (1-indexed)
                rune = tile_type.value
        
        if rune:
            if isinstance(rune, str):
                rune = rune.lower()
                if rune == "air":
                    return AIR_EMOJI
                elif rune == "water":
                    return WATER_EMOJI
                elif rune == "earth":
                    return EARTH_EMOJI
                elif rune == "fire":
                    return FIRE_EMOJI
        
        return ":question:"
    
    def _parse_tasks(self, tasks_data: List[Dict[str, Any]]) -> List[Task]:
        """
        Parse task data into Task objects
        
        Args:
            tasks_data: List of task data dictionaries
            
        Returns:
            List of Task objects
        """
        tasks = []
        
        for i, task_data in enumerate(tasks_data):
            if task_data.get("name") == "The Fremennik":
                print(task_data)
            difficulty = TileType(task_data.get("difficulty", "air"))
            points = task_data.get("points", 10)
            description = task_data.get("description", "")
            required_items = task_data.get("required_items", [])
            type = task_data.get("type", "exact_item")
            if isinstance(required_items, list):
                # Convert list of required items to TaskItem objects
                if task_data.get("name") == "The Fremennik":
                    print("Required items is a list: ", required_items)
                required_items = [
                    TaskItem(name=item.get("item_name", ""), points=item.get("points", 1))
                    for item in required_items
                ]
            elif isinstance(required_items, dict):
                # Convert single required item dict to TaskItem
                if task_data.get("name") == "The Fremennik":
                    print("Required items is a dict: ", required_items)
                required_items = TaskItem(
                    name=required_items.get("item_name", ""),
                    points=required_items.get("points", 1)
                )
            
            task = Task(
                task_id=task_data.get("id", 0),
                name=task_data.get("name", "Unknown Task"),
                type=type,
                difficulty=difficulty,
                points=points,
                required_items=required_items,
                description=description,
                is_assembly=task_data.get("is_assembly", False)
            )
            tasks.append(task)
        
        return tasks
    
    def _load_teams_from_db(self) -> None:
        """Load teams from the database"""
        db_teams = session.query(EventTeam).filter(
            EventTeam.event_id == self.event_id
        ).all()
        
        for db_team in db_teams:
            # Get team members
            team_members = session.query(EventParticipant).filter(
                EventParticipant.event_id == self.event_id,
                EventParticipant.team_id == db_team.id
            ).all()
            
            # Get player objects
            player_ids = [member.player_id for member in team_members]
            players = session.query(Player).filter(
                Player.player_id.in_(player_ids)
            ).all()
            
            # Create team object
            team = Team(
                name=db_team.name,
                players=players,
                position=int(db_team.current_location or 1),
                team_id=db_team.id,
                task_progress=db_team.task_progress,
                assembled_items=db_team.assembled_items,
                mercy_rule=db_team.mercy_rule,
                mercy_count=db_team.mercy_count,
                gold=db_team.gold,
                points=db_team.points,
                current_task=self.tasks[db_team.current_task] if db_team.current_task else None,
                turn_number=db_team.turn_number
            )
            
            # Load team inventory
            inventory_items = session.query(EventTeamInventory).filter(
                EventTeamInventory.event_team_id == db_team.id
            ).all()
            
            for inv_item in inventory_items:
                db_item = session.query(EventItems).get(inv_item.item_id)
                if db_item:
                    for _ in range(inv_item.quantity):
                        item = Item(
                            name=db_item.name,
                            cost=db_item.quantity,  # Using quantity as cost
                            effect=db_item.description or "",
                            emoji="🔮",  # Default emoji
                            item_type=ItemType(db_item.type),
                            cooldown=db_item.cooldown
                        )
                        team.inventory.append(item)
            
            self.teams.append(team)
    
    def _save_teams_to_db(self) -> None:
        """Save teams to the database"""
        for team in self.teams:
            team: Team = team
            # Skip if team has no database ID and no players
            if team.team_id is None and not team.players:
                continue
                
            if team.team_id is None:
                # Create new team in database
                db_team = EventTeam(
                    event_id=self.event_id,
                    team_name=team.name,
                    team_members=",".join(str(p.player_id) for p in team.players),
                    current_location=str(team.position),
                    previous_location="0"
                )
                session.add(db_team)
                session.flush()  # Get ID without committing
                team.team_id = db_team.id
            else:
                # Update existing team
                db_team: EventTeam = session.query(EventTeam).get(team.team_id)
                if db_team:
                    db_team.name = team.name
                    db_team.current_location = str(team.position)
                    db_team.previous_location = db_team.current_location
            
            # Update team participants
            for player in team.players:
                participant: EventParticipant = session.query(EventParticipant).filter(
                    EventParticipant.event_id == self.event_id,
                    EventParticipant.player_id == player.player_id
                ).first()
                
                if not participant:
                    # Find user_id for this player
                    user_id = session.query(Player).filter(
                        Player.player_id == player.player_id
                    ).first().user_id
                    if user_id:
                        user: User = session.query(User).filter(
                            User.user_id == user_id
                        ).first()
                        participant = EventParticipant(
                            event_id=self.event_id,
                            user_id=user.user_id,
                            player_id=player.player_id,
                            team_id=team.team_id,
                            status="active"
                        )
                        session.add(participant)
            
            # Update team inventory
            # First, clear existing inventory
            session.query(EventTeamInventory).filter(
                EventTeamInventory.event_team_id == team.team_id
            ).delete()
            
            # Group items by name and count quantities
            inventory_counts = {}
            for item in team.inventory:
                if item.name in inventory_counts:
                    inventory_counts[item.name]["count"] += 1
                else:
                    inventory_counts[item.name] = {
                        "item": item,
                        "count": 1
                    }
            
            # Add inventory items
            for item_data in inventory_counts.values():
                item = item_data["item"]
                count = item_data["count"]
                
                # Find item in database
                db_item = session.query(EventItems).filter(
                    EventItems.event_id == self.event_id,
                    EventItems.name == item.name
                ).first()
                
                if db_item:
                    inventory = EventTeamInventory(
                        event_team_id=team.team_id,
                        item_id=db_item.id,
                        quantity=count
                    )
                    session.add(inventory)
        
        # Commit all changes
        session.commit()
    
    def serialize(self) -> Dict[str, Any]:
        """
        Serialize the game state to a dictionary
        
        Returns:
            Dictionary representation of game state
        """
        return {
            "board_size": self.board_size,
            "current_team_index": self.current_team_index,
            "teams": [self._serialize_team(team) for team in self.teams],
            "tiles": [self._serialize_tile(tile) for tile in self.tiles],
            "timestamp": datetime.now().isoformat()
        }
    
    def _serialize_team(self, team: Team) -> Dict[str, Any]:
        """
        Serialize a team object
        
        Args:
            team: Team object to serialize
            
        Returns:
            Dictionary representation of team
        """
        return {
            "name": team.name,
            "position": team.position,
            "points": team.points,
            "gold": team.gold,
            "inventory": [self._serialize_item(item) for item in team.inventory],
            "cooldowns": team.cooldowns,
            "active_effects": team.active_effects,
            "players": [player.player_id for player in team.players],
            "team_id": team.team_id,
            "current_task": self._serialize_task(team.current_task) if team.current_task else None,
            "task_progress": team.task_progress,
            "assembled_items": team.assembled_items
        }
    
    def _serialize_tile(self, tile: Tile) -> Dict[str, Any]:
        """
        Serialize a tile object
        
        Args:
            tile: Tile object to serialize
            
        Returns:
            Dictionary representation of tile
        """
        return {
            "type": tile.type.value,
            "position": tile.position
        }
    
    def _serialize_item(self, item: Item) -> Dict[str, Any]:
        """
        Serialize an item object
        
        Args:
            item: Item object to serialize
            
        Returns:
            Dictionary representation of item
        """
        return {
            "name": item.name,
            "cost": item.cost,
            "effect": item.effect,
            "emoji": item.emoji,
            "item_type": item.item_type.value,
            "cooldown": item.cooldown
        }
    
    def _serialize_task(self, task: Task) -> Dict[str, Any]:
        """
        Serialize a task object
        
        Args:
            task: Task object to serialize
            
        Returns:
            Dictionary representation of task
        """
        if task is None:
            return None
            
        required_items = task.required_items
        if isinstance(required_items, list):
            serialized_items = [{"name": item.name, "points": item.points} for item in required_items]
        elif isinstance(required_items, TaskItem):
            serialized_items = {"name": required_items.name, "points": required_items.points}
        else:
            serialized_items = required_items
            
        return {
            "name": task.name,
            "difficulty": task.difficulty.value,
            "points": task.points,
            "required_items": serialized_items,
            "is_assembly": task.is_assembly,
            "type": task.type,
            "target_points": task.points
        }
    
    @classmethod
    def deserialize(cls, event_id: int, state_data: Dict[str, Any], notification_channel_id: Optional[int] = None, bot=None) -> 'BoardGame':
        """
        Create a BoardGame instance from serialized state
        
        Args:
            event_id: The ID of the event
            state_data: The serialized game state
            notification_channel_id: Optional Discord channel ID for notifications
            bot: Optional Discord bot instance
            
        Returns:
            A new BoardGame instance with the loaded state
        """
        # Create a new instance
        game = cls(event_id, notification_channel_id, bot)
        
        # Restore basic properties
        game.current_team_index = state_data.get("current_team_index", 0)
        
        # Restore tiles
        game.tiles = [
            Tile(
                position=tile_data["position"],
                type=tile_data["type"]
            )
            for tile_data in state_data.get("tiles", [])
        ]
        
        # Restore teams
        game.teams = []
        for team_data in state_data.get("teams", []):
            # Get player objects
            player_ids = team_data.get("players", [])
            players = session.query(Player).filter(
                Player.player_id.in_(player_ids)
            ).all()
            
            team = Team(
                name=team_data["name"],
                players=players,
                position=team_data["position"],
                points=team_data["points"],
                gold=team_data["gold"],
                team_id=team_data.get("team_id")
            )
            
            # Restore inventory
            for item_data in team_data.get("inventory", []):
                item = Item(
                    name=item_data["name"],
                    cost=item_data["cost"],
                    effect=item_data["effect"],
                    emoji=item_data["emoji"],
                    item_type=ItemType(item_data["item_type"]),
                    cooldown=item_data["cooldown"]
                )
                team.inventory.append(item)
            
            # Restore cooldowns and effects
            team.cooldowns = team_data.get("cooldowns", {})
            team.active_effects = team_data.get("active_effects", {})
            
            # Restore current task if any
            task_data = team_data.get("current_task")
            if task_data:
                required_items_data = task_data.get("required_items")
                if isinstance(required_items_data, list):
                    required_items = [
                        TaskItem(name=item["name"], points=item["points"])
                        for item in required_items_data
                    ]
                elif isinstance(required_items_data, dict):
                    required_items = TaskItem(
                        name=required_items_data["name"],
                        points=required_items_data["points"]
                    )
                else:
                    required_items = required_items_data
                
                team.current_task = Task(
                    id=task_data["id"],
                    name=task_data["name"],
                    difficulty=TileType(task_data["difficulty"]),
                    points=task_data["points"],
                    required_items=required_items,
                    is_assembly=task_data["is_assembly"]
                )
            
            team.task_progress = team_data.get("task_progress", 0)
            team.assembled_items = team_data.get("assembled_items", None)
            
            game.teams.append(team)
        
        return game
    
    def save_game_state(self):
        """
        Save the current game state to the database
        """
        try:
            # Convert tiles to dictionaries for JSON serialization
            tiles_dict = []
            for tile in self.tiles:
                # Convert Tile object to dictionary
                # Handle TileType enum by converting to string
                tile_dict = {
                    "position": tile.position,
                    "type": tile.type.name if hasattr(tile.type, 'name') else tile.type
                }
                tiles_dict.append(tile_dict)
            
            # Save basic game state
            game_state = {
                "tiles": tiles_dict,
                "shop_items": self.shop_items,
                "current_turn": self.current_turn
            }
            
            # Save to database
            config = session.query(EventConfig).filter(
                EventConfig.event_id == self.event_id,
                EventConfig.config_key == "game_state"
            ).order_by(EventConfig.update_number.desc()).first()
            
            update_number = 1
            if config:
                update_number = config.update_number + 1
            
            new_config = EventConfig(
                event_id=self.event_id,
                config_key="game_state",
                update_number=update_number,
                long_value=json.dumps(game_state)
            )

            teams = self.teams
            for team in teams:
                team: Team = team
                db_team = session.query(EventTeam).filter(EventTeam.name == team.name).first()
                if team.current_task:
                    db_task = session.query(EventTask).filter(EventTask.name == team.current_task.name).first()
                    db_team.current_task = db_task.id
                db_team.task_progress = team.task_progress
                db_team.assembled_items = team.assembled_items
                db_team.mercy_rule = team.mercy_rule
                db_team.mercy_count = team.mercy_count
                db_team.gold = team.gold
                db_team.current_location = team.position
                db_team.points = team.points

                session.commit()
            
            session.add(new_config)
            
            # Save team cooldowns and effects
            self._save_team_cooldowns_and_effects()
            
            session.commit()
            logger.info(f"Saved game state version {update_number} for event {self.event_id}")
            return True
        except Exception as e:
            logger.error(f"Error saving game state: {e}")
            logger.error(traceback.format_exc())
            session.rollback()
            return False

    def load_game_state(self, version: int = None) -> bool:
        """
        Load game state from database
        
        Args:
            version: Optional specific version to load, defaults to latest
            
        Returns:
            True if state was loaded successfully, False otherwise
        """
        try:
            # Load event status first
            self._load_event_status()
            
            # If no version specified, get the latest
            if version is None:
                latest_config = session.query(EventConfig).filter(
                    EventConfig.event_id == self.event_id,
                    EventConfig.config_key == "game_state"
                ).order_by(EventConfig.update_number.desc()).first()
                
                if latest_config:
                    version = latest_config.update_number
                else:
                    # No saved state exists, initialize a new game
                    logger.info(f"No saved state found for event {self.event_id}, initializing new game")
                    self._initialize_new_game()
                    
                    # Only save if event is active
                    if self._check_event_active():
                        self.save_game_state()
                    
                    return True
            
            # Get the specified version
            config = session.query(EventConfig).filter(
                EventConfig.event_id == self.event_id,
                EventConfig.config_key == "game_state",
                EventConfig.update_number == version
            ).first()
            
            if not config:
                logger.error(f"Game state version {version} not found for event {self.event_id}")
                return False
            
            # Parse the game state
            try:
                game_state = json.loads(config.long_value)
                
                # Load tiles
                if "tiles" in game_state:
                    # Convert tile dictionaries to Tile objects
                    self.tiles = []
                    for tile_dict in game_state["tiles"]:
                        # Create Tile object - convert string type to TileType enum
                        # Handle case conversion for enum lookup
                        if isinstance(tile_dict["type"], str):
                            try:
                                # Try direct lookup first
                                tile_type = TileType[tile_dict["type"]]
                            except KeyError:
                                # Try uppercase lookup
                                try:
                                    tile_type = TileType[tile_dict["type"].upper()]
                                except KeyError:
                                    # If all else fails, regenerate tiles
                                    logger.warning(f"Unknown tile type: {tile_dict['type']}, regenerating tiles")
                                    self._generate_tiles(self.config.board_size)
                                    break
                        else:
                            tile_type = tile_dict["type"]
                        
                        tile = Tile(
                            type=tile_type,
                            position=tile_dict["position"]
                        )
                        self.tiles.append(tile)
                    
                    # If no tiles were loaded, generate them
                    if not self.tiles:
                        self._generate_tiles(self.config.board_size)
                else:
                    # Generate tiles if not in saved state
                    self._generate_tiles(self.config.board_size)
                
                # Load shop items
                if "shop_items" in game_state:
                    self.shop_items = game_state["shop_items"]
                else:
                    # Generate shop items if not in saved state
                    self._generate_shop_items()
                
                # Load current turn
                if "current_turn" in game_state:
                    self.current_turn = game_state["current_turn"]
                else:
                    self.current_turn = 0
                
                # Always load teams from database to ensure consistency
                self._load_teams_from_database()

                self._load_tasks()
                
                # Load team cooldowns and effects
                self._load_team_cooldowns_and_effects()
                
                logger.info(f"Loaded game state version {version} for event {self.event_id}")
                return True
            except json.JSONDecodeError:
                logger.error(f"Invalid JSON in game state for event {self.event_id}")
                return False
        except Exception as e:
            logger.error(f"Error loading game state: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def _initialize_new_game(self):
        """Initialize a new game with default settings"""
        # Use configuration values
        board_size = self.config.board_size
        starting_gold = self.config.starting_gold
        
        # Generate tiles based on board size
        self._generate_tiles(board_size)
        
        # Load teams from database with starting gold
        self._load_teams_from_database(starting_gold)
        
        # Generate shop items if shop is enabled
        if self.config.shop_enabled:
            self._generate_shop_items()
        
        # Set current turn to first team if any teams exist
        if self.teams:
            self.current_turn = 0
        
        
        logger.info(f"Game initialized with {len(self.tiles)} tiles and {len(self.teams)} teams")
    
    def _load_teams_from_database(self, default_gold=None):
        """
        Load teams from the database
        
        Args:
            default_gold: Default gold to assign if not specified in team record
        """
        try:
            # Clear existing teams first
            self.teams = []
            
            # Get teams from database
            db_teams = session.query(EventTeam).filter(
                EventTeam.event_id == self.event_id
            ).all()
            
            logger.info(f"Loading {len(db_teams)} teams from database for event {self.event_id}")
            
            for db_team in db_teams:
                db_team: EventTeam = db_team
                # Create team object using the Team dataclass
                team = Team(
                    name=db_team.name,
                    players=[],  # Will populate below
                    position=db_team.current_location if hasattr(db_team, 'current_location') else 0,
                    points=db_team.points if hasattr(db_team, 'points') else 0,
                    gold=db_team.gold if hasattr(db_team, 'gold') else (default_gold or self.config.starting_gold),
                    inventory=[],  # Will populate below
                    cooldowns=[], # Will populate below
                    active_effects=[], # Will populate below
                    current_task=db_team.current_task,
                    task_progress=db_team.task_progress,
                    assembled_items=db_team.assembled_items,
                    team_id=db_team.id,  # Store the database ID
                    current_task_id=db_team.current_task,  # Store the task ID
                    mercy_rule=db_team.mercy_rule if hasattr(db_team, 'mercy_rule') else None,
                    mercy_count=db_team.mercy_count if hasattr(db_team, 'mercy_count') else 0
                )
                
                # Get team members
                members = session.query(EventParticipant).filter(
                    EventParticipant.team_id == db_team.id
                ).all()
                
                for member in members:
                    player = session.query(Player).filter(
                        Player.player_id == member.player_id
                    ).first()
                    
                    if player:
                        user = session.query(User).filter(
                            User.user_id == player.user_id
                        ).first()
                        
                        if user:
                            # Add player to team
                            team.players.append(member)
                
                # Get team inventory
                inventory_items = session.query(EventTeamInventory, EventItems).join(
                    EventItems, EventTeamInventory.item_id == EventItems.id
                ).filter(
                    EventTeamInventory.event_team_id == db_team.id
                ).all()
                
                for inv, item_data in inventory_items:
                    for _ in range(inv.quantity):
                        # Add item to team inventory
                        item = Item(
                            id=item_data.id,
                            name=item_data.name,
                            cost=item_data.cost,
                            effect=item_data.effect,
                            emoji=item_data.emoji,
                            type=item_data.type if hasattr(item_data, 'type') else "consumable",
                            cooldown=item_data.cooldown if hasattr(item_data, 'cooldown') else 0
                        )
                        team.inventory.append(item)
                
                self.teams.append(team)
            
            # Load team cooldowns and effects
            self._load_team_cooldowns_and_effects()
            logger.info(f"Loaded {len(self.teams)} teams with {sum(len(t.players) for t in self.teams)} players from database")
            
            # Log team names for debugging
            team_names = [team.name for team in self.teams]
            logger.info(f"Loaded teams: {', '.join(team_names)}")
            
            return True
        except Exception as e:
            logger.error(f"Error loading teams from database: {e}")
            logger.error(traceback.format_exc())
            return False

    def _generate_shop_items(self):
        """Generate shop items"""
        # TODO: Implement shop generation
        pass
    
        
    def _load_effects(self, effects: str) -> Dict[str, int]:
        """Load effects from database"""
        return json.loads(effects)

    def create_team(self, team_name: str) -> Team:
        """
        Create a new team
        
        Args:
            team_name: Name of the team
            
        Returns:
            The created Team object
        """
        # Check if team already exists
        for team in self.teams:
            if team.name == team_name:
                return team
        
        # Create new team
        team = Team(name=team_name, players=[])
        self.teams.append(team)
        
        # Save to database
        db_team = EventTeam(
            event_id=self.event_id,
            team_name=team_name,
            team_members="",
            current_position="0",
            previous_position="0"
        )
        session.add(db_team)
        session.commit()
        
        team.team_id = db_team.id
        self.save_game_state()
        
        return team
    
    def join_team(self, player_id: int, player_name: str, discord_id: str, team_name: str) -> bool:
        """
        Add a player to a team
        
        Args:
            player_id: ID of the player
            player_name: Name of the player
            discord_id: Discord ID of the player
            team_name: Name of the team
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if player is already in a team
            current_team = self.is_player_in_team(player_id)
            
            if current_team:
                if current_team == team_name:
                    logger.info(f"Player {player_name} is already in team '{team_name}'")
                    return False
                else:
                    logger.info(f"Player {player_name} is in team '{current_team}', must leave first")
                    return False
            
            # Find team in memory
            team = next((t for t in self.teams if t["name"] == team_name), None)
            
            if not team:
                logger.error(f"Team '{team_name}' not found in game state.")
                return False
            
            # Find team in database
            db_team = session.query(EventTeam).filter(
                EventTeam.event_id == self.event_id,
                EventTeam.name == team_name
            ).first()
            
            if not db_team:
                logger.error(f"Team '{team_name}' not found in database.")
                return False
            
            # Add player to team in memory
            team["members"].append({
                "id": player_id,
                "name": player_name,
                "discord_id": discord_id
            })
            
            # Add player to team in database
            participant = EventParticipant(
                team_id=db_team.id,
                player_id=player_id
            )
            
            session.add(participant)
            session.commit()
            
            logger.info(f"Added player {player_name} to team '{team_name}'")
            return True
        except Exception as e:
            logger.error(f"Error adding player to team: {e}")
            logger.error(traceback.format_exc())
            session.rollback()
            return False
    
    def remove_player_from_team(self, team_name: str, player_id: int) -> bool:
        """
        Remove a player from a team
        
        Args:
            team_name: Name of the team
            player_id: ID of the player to remove
            
        Returns:
            True if player was removed successfully, False otherwise
        """
        # Find team
        team = None
        for t in self.teams:
            if t["name"] == team_name:
                team = t
                break
        
        if not team:
            return False
        
        # Remove player from team
        for i, player in enumerate(team["members"]):
            if player["id"] == player_id:
                team["members"].pop(i)
                
                # Update database
                participant = session.query(EventParticipant).filter(
                    EventParticipant.event_id == self.event_id,
                    EventParticipant.player_id == player_id
                ).first()
                
                if participant:
                    session.delete(participant)
                    session.commit()
                
                self.save_game_state()
                return True
        
        return False
    
    def get_team(self, team_name: str) -> Optional[Team]:
        """
        Get a team by name
        
        Args:
            team_name: Name of the team
            
        Returns:
            Team object or None if not found
        """
        for mem_team in self.teams:
            team: Team = mem_team
            if team.name == team_name:
                return team
        return None
    
    def move_team(self, team_name: str, spaces: int) -> Tile:
        """
        Move a team forward on the board and return the tile they land on
        
        Args:
            team_name: Name of the team to move
            spaces: Number of spaces to move
            
        Returns:
            The Tile object the team landed on
        """
        team: Team = self.get_team(team_name)
        if not team:
            raise ValueError(f"Team {team_name} not found")
            
        # Calculate new position
        new_position = int(team.position) + int(spaces)
        
        # Ensure position stays within bounds (0-99)
        if new_position < 0:
            new_position = 0
        elif new_position > self.config.board_size:
            new_position = self.config.board_size
        
        # Update team position
        team.position = new_position
        
        # Update database
        if team.team_id:
            db_team: EventTeam = session.query(EventTeam).get(team.team_id)
            if db_team:
                db_team.previous_location = db_team.current_location
                db_team.current_location = str(new_position)
                session.commit()
        
        self.save_game_state()
        for tile in self.tiles:
            tile: Tile = tile
            if tile.position == new_position:
                return tile
        return None
    
    def set_team_position(self, team_name: str, position: int) -> None:
        """
        Set the position for a specific team
        
        Args:
            team_name: Name of the team
            position: New position for the team
        """
        team = self.get_team(team_name)
        if not team:
            raise ValueError(f"Team {team_name} not found")
            
        # Ensure position is within bounds (0-99)
        position = max(0, min(position, self.config.board_size - 1))
        
        # Update team position
        team.position = position
        
        # Update database
        if team.team_id:
            db_team: EventTeam = session.query(EventTeam).get(team.team_id)
            if db_team:
                db_team.previous_location = db_team.current_location
                db_team.current_location = str(position)
                session.commit()
        
        self.save_game_state()
    
    def add_points_to_team(self, team_name: str, points: int) -> int:
        """
        Add points to a team
        
        Args:
            team_name: Name of the team
            points: Number of points to add
            
        Returns:
            New total points
        """
        team = self.get_team(team_name)
        if not team:
            raise ValueError(f"Team {team_name} not found")
            
        team.points += points
        self.save_game_state()
        return team.points
    
    def add_gold_to_team(self, team_name: str, gold: int) -> int:
        """
        Add gold to a team
        
        Args:
            team_name: Name of the team
            gold: Amount of gold to add
            
        Returns:
            New total gold
        """
        team = self.get_team(team_name)
        if not team:
            raise ValueError(f"Team {team_name} not found")
            
        team.gold += gold
        self.save_game_state()  
        return team.gold
    
    def add_item_to_team(self, team_name: str, item_name: str) -> bool:
        """
        Add an item to a team's inventory
        
        Args:
            team_name: Name of the team
            item_name: Name of the item to add
            
        Returns:
            True if item was added successfully, False otherwise
        """
        team = self.get_team(team_name)
        if not team:
            return False
            
        # Find the item in the shop
        item = None
        for shop_item in self.shop_items:
            if shop_item.name == item_name:
                item = shop_item
                break
                
        if not item:
            return False
            
        # Add item to team inventory
        team.inventory.append(item)
        
        # Update database
        if team.team_id:
            # Find item ID
            db_item = session.query(EventItems).filter(
                EventItems.event_id == self.event_id,
                EventItems.name == item_name
            ).first()
            
            if db_item:
                # Check if item already exists in inventory
                existing = session.query(EventTeamInventory).filter(
                    EventTeamInventory.event_team_id == team.team_id,
                    EventTeamInventory.item_id == db_item.id
                ).first()
                
                if existing:
                    existing.quantity += 1
                else:
                    inventory_item = EventTeamInventory(
                        event_team_id=team.team_id,
                        item_id=db_item.id,
                        quantity=1
                    )
                    session.add(inventory_item)
                
                session.commit()
        
        self.save_game_state()
        return True
    
    def remove_item_from_team(self, team_name: str, item_name: str) -> bool:
        """
        Remove an item from a team's inventory
        
        Args:
            team_name: Name of the team
            item_name: Name of the item to remove
            
        Returns:
            True if item was removed successfully, False otherwise
        """
        team = self.get_team(team_name)
        if not team:
            return False
            
        # Find and remove the item
        for i, item in enumerate(team.inventory):
            if item.name == item_name:
                team.inventory.pop(i)
                
                # Update database
                if team.team_id:
                    db_item = session.query(EventItems).filter(
                        EventItems.event_id == self.event_id,
                        EventItems.name == item_name
                    ).first()
                    
                    if db_item:
                        inventory_item = session.query(EventTeamInventory).filter(
                            EventTeamInventory.event_team_id == team.team_id,
                            EventTeamInventory.item_id == db_item.id
                        ).first()
                        
                        if inventory_item:
                            if inventory_item.quantity > 1:
                                inventory_item.quantity -= 1
                            else:
                                session.delete(inventory_item)
                            
                            session.commit()
                
                self.save_game_state()
                return True
                
        return False
    
    def use_item(self, team_name: str, item_name: str) -> Tuple[bool, str, Optional[Item]]:
        """
        Use an item from a team's inventory
        
        Args:
            team_name: Name of the team
            item_name: Name of the item to use
            
        Returns:
            Tuple of (success, effect_description, item_used)
        """
        team = self.get_team(team_name)
        if not team:
            return False, "Team not found", None
            
        # Find the item
        item = None
        for i, inv_item in enumerate(team.inventory):
            if inv_item.name == item_name:
                item = inv_item
                break
                
        if not item:
            return False, "Item not found in inventory", None
            
        # Check cooldowns
        if item_name in team.cooldowns and team.cooldowns[item_name] > 0:
            return False, f"Item is on cooldown for {team.cooldowns[item_name]} more turns", item
            
        # Apply item effect (this would be implemented in subclasses)
        effect_description = self.apply_item_effect(team_name, item)
        
        # Set cooldown
        team.cooldowns[item_name] = item.cooldown
        
        # Remove item from inventory
        self.remove_item_from_team(team_name, item_name)
        
        self.save_game_state()
        return True, effect_description, item
    
    def apply_item_effect(self, team_name: str, item: Item) -> str:
        """
        Apply an item's effect
        
        Args:
            team_name: Name of the team
            item: The item to apply
            
        Returns:
            Description of the effect
        """
        # This is a placeholder - subclasses should implement specific effects
        return f"Used {item['name']} - {item['effect']}"
    
    def next_turn(self) -> str:
        """
        Advance to the next team's turn
        
        Returns:
            Name of the team whose turn it is now
        """
        # Decrement cooldowns for all teams
        for team in self.teams:
            team: Team = team
            for item_name in list(team.cooldowns.keys()):
                if team.cooldowns[item_name] > 0:
                    team.cooldowns[item_name] -= 1
                    if team.cooldowns[item_name] <= 0:
                        del team.cooldowns[item_name]
            
            # Decrement active effects
            for effect_name in list(team.active_effects.keys()):
                if team.active_effects[effect_name] > 0:
                    team.active_effects[effect_name] -= 1
                    if team.active_effects[effect_name] <= 0:
                        del team.active_effects[effect_name]
        
        # Move to next team
        self.current_team_index = (self.current_team_index + 1) % len(self.teams)
        
        self.save_game_state()
        return self.teams[self.current_team_index].name
    
    def get_current_team(self) -> Optional[Team]:
        """
        Get the team whose turn it currently is
        
        Returns:
            The current Team object or None if no teams exist
        """
        if not self.teams:
            return None
        return self.teams[self.current_team_index]
    
    def generate_task(self, tile_type: TileType) -> Task:
        """
        Generate a task for a specific tile type
        
        Args:
            tile_type: The type of tile
            
        Returns:
            A Task object
        """
        # Debug the task difficulties
        print(f"Looking for tasks with difficulty {tile_type}")
        
        # Filter tasks by difficulty matching the tile type
        matching_tasks = []
        for task in self.tasks:
            # Check if task.difficulty is a string representation of TileType
            if isinstance(task.difficulty, str) and task.difficulty.startswith('TileType.'):
                # Extract the enum value from the string (e.g., 'TileType.AIR' -> 'AIR')
                difficulty_str = task.difficulty.split('.')[1] if '.' in task.difficulty else task.difficulty
                # Compare with tile_type.name
                if difficulty_str.upper() == tile_type.name:
                    matching_tasks.append(task)
            # Check if task.difficulty is a TileType enum
            elif isinstance(task.difficulty, TileType) and task.difficulty == tile_type:
                matching_tasks.append(task)
            # Check if task.difficulty is a string value that matches tile_type.value
            elif isinstance(task.difficulty, str) and task.difficulty.lower() == tile_type.value.lower():
                matching_tasks.append(task)
        
        print(f"Found {len(matching_tasks)} tasks for tile type {tile_type}")
        
        if not matching_tasks:
            # Fallback to any task if no matching ones found
            matching_tasks = self.tasks
            print(f"No matching tasks found, using all tasks")
        
        # Select a random task
        if len(matching_tasks) > 0:
            task = random.choice(matching_tasks)
        else:
            task = None
        
        print(f"Generated task: {task}")
        return task
    
    def assign_task(self, team_name: str, tile_index: int) -> Optional[Task]:
        """
        Assign a task to a team based on the tile they landed on
        
        Args:
            team_name: Name of the team
            tile_index: Index of the tile
            
        Returns:
            Task object if assigned, None otherwise
        """
        try:
            # Get the team
            team = self.get_team(team_name)
            if not team:
                logger.error(f"Team '{team_name}' not found")
                return None
            
            # Get the tile
            if tile_index < 0 or tile_index >= len(self.tiles):
                logger.error(f"Invalid tile index: {tile_index}")
                return None
            
            tile = self.tiles[tile_index]
            
            # Generate a task based on tile type
            task = self.generate_task(tile.type)
            
            if task:
                # Save the task to the database to get an ID
                print("Finding database task for task: ", task.name)
                db_task = session.query(EventTask).filter(EventTask.name == task.name).first()
                # Update the task with the database ID
                if not db_task:
                    db_task = EventTask(
                        event_id=self.event_id,
                        type=task.type,
                        name=task.name,
                        description=task.description,
                        difficulty=task.difficulty,
                        points=task.points,
                        required_items=[event_item.name + "," for event_item in task.required_items],
                        is_assembly=task.is_assembly
                    )
                    session.add(db_task)
                    session.commit()
                    print(f"Task not found in database for event {self.event_id}, created new task")
                task.id = db_task.id
                
                # Assign the task to the team
                team.current_task = task
                team.current_task_id = task.id
                team.task_progress = 0
                
                # Set mercy rule if applicable
                if hasattr(team, 'mercy_rule'):
                    team.mercy_rule = datetime.now() + timedelta(days=1)
                
                # Reset assembled items for new task
                if hasattr(team, 'assembled_items'):
                    team.assembled_items = None
                
                # Save the updated team state
                self._save_teams_to_database()
                
                logger.info(f"Assigned task {task.name} (ID: {task.id}) to team {team_name}")
                return task
            
            return None
        except Exception as e:
            logger.error(f"Error assigning task: {e}")
            logger.error(traceback.format_exc())
            return None
    
    def check_task_completion(self, team_name: str, items: List[str]) -> bool:
        """
        Check if a team has completed their current task
        
        Args:
            team_name: Name of the team
            items: List of items submitted
            
        Returns:
            True if task is completed, False otherwise
        """
        try:
            team = self.get_team(team_name)
            if not team or not team.current_task:
                return False
            
            task = team.current_task
            task_type = getattr(task, 'type', 'exact_item')  # Default to exact_item if not specified
            
            # Convert items list to a counter for easier counting
            item_counter = Counter(items)
            
            if task_type == "exact_item":
                # Check if all required items are present in the required quantities
                for required_item in task.required_items:
                    item_id = required_item.get('item_id')
                    quantity = required_item.get('quantity', 1)
                    
                    if item_counter.get(item_id, 0) < quantity:
                        return False
                return True
            
            elif task_type == "assembly":
                # For assembly tasks, check if at least one of each component is present
                for required_item in task.required_items:
                    item_id = required_item.get('item_id')
                    
                    if item_counter.get(item_id, 0) < 1:
                        return False
                return True
            
            elif task_type == "point_collection":
                # Calculate points from submitted items
                total_points = 0
                for required_item in task.required_items:
                    item_id = required_item.get('item_id')
                    points_per_item = required_item.get('points', 1)
                    
                    count = item_counter.get(item_id, 0)
                    total_points += count * points_per_item
                
                # Check if enough points have been collected
                target_points = getattr(task, 'target_points', 10)
                return total_points >= target_points
            
            elif task_type == "any_of":
                # Check if any of the required items is present
                for required_item in task.required_items:
                    item_id = required_item.get('item_id')
                    quantity = required_item.get('quantity', 1)
                    
                    if item_counter.get(item_id, 0) >= quantity:
                        return True
                return False
            
            # Default case - unknown task type
            logger.warning(f"Unknown task type: {task_type}")
            return False
            
        except Exception as e:
            logger.error(f"Error checking task completion: {e}")
            logger.error(traceback.format_exc())
            return False

    def roll_and_move(self, team_name: str):
        """
        Roll dice and move a team
        
        Args:
            team_name: Name of the team
            
        Returns:
            Tuple of (roll result, new tile, task if assigned)
        """
        # Only allow if event is active
        if not self._check_event_active():
            raise ValueError(f"Cannot roll dice for inactive event {self.event_id}")
        
        # Verify game state
        if not self.verify_game_state():
            raise ValueError(f"Game state is invalid for event {self.event_id}")
        
        # Find team
        team = next((t for t in self.teams if t.name == team_name), None)
        
        if not team:
            # Try to reload teams from database
            logger.warning(f"Team '{team_name}' not found in game state, reloading teams")
            self._load_teams_from_database()
            
            # Check again
            team = next((t for t in self.teams if t.name == team_name), None)
            
            if not team:
                raise ValueError(f"Team '{team_name}' not found in game state")
        if team.current_task:
            return False, None, None
        # Roll dice based on configuration
        dice_count = self.config.number_of_dice
        dice_sides = self.config.die_sides
        
        roll_result, total = self._roll_dice(dice_count, dice_sides)

        if team:
            team: Team = team
            team.mercy_rule = datetime.now() + timedelta(days=1) + (timedelta(hours=12) * team.mercy_count)
            db_team = session.query(EventTeam).filter(EventTeam.name == team_name).first()
            db_team.mercy_rule = team.mercy_rule
            db_team.mercy_count = team.mercy_count
            db_team.previous_location = team.position
            session.commit()
            new_tile = self.move_team(team_name, total)
            task = self.assign_task(team_name, new_tile.position)
            
            # If a task was assigned, update the team's task ID
            if task and hasattr(task, 'id'):
                team.current_task_id = task.id
                db_team.current_task = task.id
                session.commit()
            
            return roll_result, new_tile, task
        else:
            raise ValueError(f"Team '{team_name}' not found in game state")
    
    def is_player_in_team(self, player_id: int) -> Optional[str]:
        """
        Check if a player is in a team
        
        Args:
            player_id: ID of the player
            
        Returns:
            Team name if player is in a team, None otherwise
        """
        try:
            # Check database first
            participant = session.query(EventParticipant).join(
                EventTeam, EventParticipant.team_id == EventTeam.id
            ).filter(
                EventParticipant.player_id == player_id,
                EventTeam.event_id == self.event_id
            ).first()
            
            if participant:
                team = session.query(EventTeam).filter(
                    EventTeam.id == participant.team_id
                ).first()
                
                if team:
                    # Verify team exists in game state
                    team_in_memory = next((t for t in self.teams if t.name == team.name), None)
                    
                    if not team_in_memory:
                        logger.warning(f"Team '{team.name}' exists in database but not in game state. Reloading teams.")
                        self._load_teams_from_database()
                        
                        # Check again after reload
                        team_in_memory = next((t for t in self.teams if t.name == team.name), None)
                        
                        if not team_in_memory:
                            logger.error(f"Team '{team.name}' still not found in game state after reload.")
                            # Team doesn't exist in memory, remove from database to fix inconsistency
                            logger.info(f"Removing player {player_id} from non-existent team '{team.name}'")
                            session.delete(participant)
                            session.commit()
                        
                        return None
                
                    return team.name
            
            return None
        except Exception as e:
            logger.error(f"Error checking player team membership: {e}")
            logger.error(traceback.format_exc())
            return None

    def leave_team(self, player_id: int, player_name: str) -> bool:
        """
        Remove a player from their team
        
        Args:
            player_id: ID of the player
            player_name: Name of the player
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if player is in a team
            current_team = self.is_player_in_team(player_id)
            
            if not current_team:
                logger.info(f"Player {player_name} is not in a team")
                return False
            
            # Find team in memory
            team = next((t for t in self.teams if t.name == current_team), None)
            
            # Remove from database first
            participant = session.query(EventParticipant).join(
                EventTeam, EventParticipant.team_id == EventTeam.id
            ).filter(
                EventParticipant.player_id == player_id,
                EventTeam.event_id == self.event_id
            ).first()
            
            if participant:
                session.delete(participant)
                session.commit()
            
            # Remove from memory if team exists
            if team:
                team.members = [m for m in team.members if m.player_id != player_id]
            
            logger.info(f"Removed player {player_name} from team '{current_team}'")
            return True
        except Exception as e:
            logger.error(f"Error removing player from team: {e}")
            logger.error(traceback.format_exc())
            session.rollback()
            return False

    def verify_game_state(self):
        """
        Verify and fix the game state
        
        Returns:
            True if game state is valid, False otherwise
        """
        try:
            # Check if teams are loaded
            if not self.teams:
                logger.warning("No teams loaded in game state, attempting to load from database")
                self._load_teams_from_database()
                
                # If still no teams, something is wrong
                if not self.teams:
                    logger.error("No teams found in database")
                    return False
            
            # Check if tiles are generated
            if not self.tiles:
                logger.warning("No tiles generated, generating default tiles")
                self._generate_tiles(self.config.board_size)
            
            # Check if shop items are generated
            if self.config.shop_enabled and not self.shop_items:
                logger.warning("Shop enabled but no items generated, generating default items")
                self._generate_shop_items()
            
            # Verify each team has valid data
            for team in self.teams:
                team: Team = team
                # Ensure team has all required fields
                if team.name is None:
                    logger.error(f"Team {team.name} has no name")
                    return False
                if team._get_players() is None:
                    logger.error(f"Team {team.name} has no players")
                    pass
                if team.position is None:
                    logger.error(f"Team {team.name} has no position")
                    pass
                if team.points is None:
                    logger.error(f"Team {team.name} has no points")
                    pass
                if team._get_inventory() is None:
                    logger.error(f"Team {team.name} has no inventory")
                    pass
                    
            
            logger.info("Game state verified successfully")
            return True
        except Exception as e:
            logger.error(f"Error verifying game state: {e}")
            logger.error(traceback.format_exc())
            return False

    def initialize_game(self):
        """
        Initialize the game state for a new or existing game
        
        Returns:
            True if initialization was successful, False otherwise
        """
        try:
            # Load event status
            self._load_event_status()
            
            # Check if event is active
            if not self._check_event_active():
                logger.warning(f"Attempted to initialize inactive event {self.event_id}")
                return False
            
            # Try to load existing game state
            loaded = self.load_game_state()
            
            if not loaded:
                # If loading failed, initialize a new game
                logger.info(f"Failed to load existing game state, initializing new game for event {self.event_id}")
                self._initialize_new_game()
            
            # Verify and fix game state
            self.verify_game_state()
            
            # Save the game state
            self.save_game_state()
            
            logger.info(f"Game initialized successfully for event {self.event_id}")
            return True
        except Exception as e:
            logger.error(f"Error initializing game: {e}")
            logger.error(traceback.format_exc())
            return False

    def _save_team_cooldowns_and_effects(self):
        """
        Save team cooldowns and effects to the database
        """
        try:
            # First, delete existing cooldowns and effects
            for team in self.teams:
                team: Team = team
                db_team = session.query(EventTeam).filter(
                    EventTeam.event_id == self.event_id,
                    EventTeam.name == team.name
                ).first()
                
                if not db_team:
                    logger.warning(f"Team {team.name} not found in database, skipping cooldown/effect save")
                    continue
                
                # Delete existing cooldowns
                session.query(EventTeamCooldown).filter(
                    EventTeamCooldown.team_id == db_team.id
                ).delete()
                
                # Delete existing effects
                session.query(EventTeamEffect).filter(
                    EventTeamEffect.team_id == db_team.id
                ).delete()
                
                # Save cooldowns - check if it's a list or dict
                if hasattr(team, 'cooldowns'):
                    if isinstance(team.cooldowns, dict):
                        # Handle dictionary of cooldowns
                        for cooldown_name, turns in team.cooldowns.items():
                            cooldown = EventTeamCooldown(
                                team_id=db_team.id,
                                cooldown_name=cooldown_name,
                                remaining_turns=turns
                            )
                            session.add(cooldown)
                    elif isinstance(team.cooldowns, list):
                        # Handle list of cooldowns
                        for cooldown in team.cooldowns:
                            # Assuming each cooldown is a dict or object with name and turns
                            if isinstance(cooldown, dict):
                                cooldown_obj = EventTeamCooldown(
                                    team_id=db_team.id,
                                    cooldown_name=cooldown.get('name', 'unknown'),
                                    remaining_turns=cooldown.get('turns', 0)
                                )
                            else:
                                # Assuming it's an object with name and turns attributes
                                cooldown_obj = EventTeamCooldown(
                                    team_id=db_team.id,
                                    cooldown_name=getattr(cooldown, 'name', 'unknown'),
                                    remaining_turns=getattr(cooldown, 'turns', 0)
                                )
                            session.add(cooldown_obj)
                
                # Save effects - check if it's a list or dict
                if hasattr(team, 'active_effects'):
                    if isinstance(team.active_effects, dict):
                        # Handle dictionary of effects
                        for effect_name, turns in team.active_effects.items():
                            effect = EventTeamEffect(
                                team_id=db_team.id,
                                effect_name=effect_name,
                                remaining_turns=turns
                            )
                            session.add(effect)
                    elif isinstance(team.active_effects, list):
                        # Handle list of effects
                        for effect in team.active_effects:
                            # Assuming each effect is a dict or object with name and turns
                            if isinstance(effect, dict):
                                effect_obj = EventTeamEffect(
                                    team_id=db_team.id,
                                    effect_name=effect.get('name', 'unknown'),
                                    remaining_turns=effect.get('turns', 0)
                                )
                            else:
                                # Assuming it's an object with name and turns attributes
                                effect_obj = EventTeamEffect(
                                    team_id=db_team.id,
                                    effect_name=getattr(effect, 'name', 'unknown'),
                                    remaining_turns=getattr(effect, 'turns', 0)
                                )
                            session.add(effect_obj)
            
            session.commit()
            logger.info(f"Saved team cooldowns and effects for event {self.event_id}")
            return True
        except Exception as e:
            logger.error(f"Error saving team cooldowns and effects: {e}")
            logger.error(traceback.format_exc())
            session.rollback()
            return False

    def _load_team_cooldowns_and_effects(self):
        """
        Load team cooldowns and effects from the database
        """
        try:
            for team in self.teams:
                db_team = session.query(EventTeam).filter(
                    EventTeam.event_id == self.event_id,
                    EventTeam.name == team.name
                ).first()
                
                if not db_team:
                    logger.warning(f"Team {team.name} not found in database, skipping cooldown/effect load")
                    continue
                
                # Initialize cooldowns and effects as dictionaries if they don't exist
                if not hasattr(team, 'cooldowns') or team.cooldowns is None:
                    team.cooldowns = {}
                elif isinstance(team.cooldowns, list):
                    # Convert list to dictionary for easier updates
                    cooldowns_dict = {}
                    for cooldown in team.cooldowns:
                        if isinstance(cooldown, dict):
                            cooldowns_dict[cooldown.get('name', 'unknown')] = cooldown.get('turns', 0)
                        else:
                            cooldowns_dict[getattr(cooldown, 'name', 'unknown')] = getattr(cooldown, 'turns', 0)
                    team.cooldowns = cooldowns_dict
                
                if not hasattr(team, 'active_effects') or team.active_effects is None:
                    team.active_effects = {}
                elif isinstance(team.active_effects, list):
                    # Convert list to dictionary for easier updates
                    effects_dict = {}
                    for effect in team.active_effects:
                        if isinstance(effect, dict):
                            effects_dict[effect.get('name', 'unknown')] = effect.get('turns', 0)
                        else:
                            effects_dict[getattr(effect, 'name', 'unknown')] = getattr(effect, 'turns', 0)
                    team.active_effects = effects_dict
                
                # Load cooldowns
                cooldowns = session.query(EventTeamCooldown).filter(
                    EventTeamCooldown.team_id == db_team.id
                ).all()
                
                for cooldown in cooldowns:
                    team.cooldowns[cooldown.cooldown_name] = cooldown.remaining_turns
                
                # Load effects
                effects = session.query(EventTeamEffect).filter(
                    EventTeamEffect.team_id == db_team.id
                ).all()
                
                for effect in effects:
                    team.active_effects[effect.effect_name] = effect.remaining_turns
            
            logger.info(f"Loaded team cooldowns and effects for event {self.event_id}")
            return True
        except Exception as e:
            logger.error(f"Error loading team cooldowns and effects: {e}")
            logger.error(traceback.format_exc())
            return False

    def _save_teams_to_database(self):
        """
        Save teams to the database
        """
        try:
            for team in self.teams:
                db_team = session.query(EventTeam).filter(
                    EventTeam.event_id == self.event_id,
                    EventTeam.name == team.name
                ).first()
                
                if db_team:
                    # Update existing team
                    db_team.current_location = str(team.position)
                    db_team.points = team.points
                    
                    # Save current task ID (not the full task object)
                    if team.current_task:
                        db_team.current_task_id = team.current_task_id or team.current_task.task_id
                    else:
                        db_team.current_task_id = None
                    
                    db_team.task_progress = team.task_progress
                    
                    # Save mercy rule if it exists
                    if hasattr(team, 'mercy_rule') and team.mercy_rule:
                        db_team.mercy_rule = team.mercy_rule
                    
                    # Save mercy count if it exists
                    if hasattr(team, 'mercy_count'):
                        db_team.mercy_count = team.mercy_count
                    
                    # Save assembled items if they exist
                    if hasattr(team, 'assembled_items') and team.assembled_items:
                        db_team.assembled_items = team.assembled_items
                else:
                    # Create new team
                    new_team = EventTeam(
                        event_id=self.event_id,
                        name=team.name,
                        current_location=str(team.position),
                        points=team.points,
                        current_task_id=team.current_task_id if team.current_task_id else 
                                       (team.current_task.task_id if team.current_task else None),
                        task_progress=team.task_progress,
                        mercy_rule=team.mercy_rule if hasattr(team, 'mercy_rule') else None,
                        mercy_count=team.mercy_count if hasattr(team, 'mercy_count') else 0,
                        assembled_items=team.assembled_items if hasattr(team, 'assembled_items') else None
                    )
                    session.add(new_team)
            
            session.commit()
            logger.info(f"Saved teams for event {self.event_id}")
            return True
        except Exception as e:
            logger.error(f"Error saving teams: {e}")
            logger.error(traceback.format_exc())
            session.rollback()
            return False

    def _load_teams_from_database(self):
        """
        Load teams from the database
        """
        try:
            db_teams = session.query(EventTeam).filter(
                EventTeam.event_id == self.event_id
            ).all()
            
            self.teams = []
            
            for db_team in db_teams:
                # Create team object
                team = Team(
                    name=db_team.name,
                    position=int(db_team.current_location) if db_team.current_location else 0,
                    points=db_team.points or 0,
                    team_id=db_team.id,
                    current_task=None,  # Will be loaded below if needed
                    current_task_id=db_team.current_task,  # Store the task ID
                    task_progress=db_team.task_progress or 0,
                    cooldowns={},
                    active_effects={},
                    mercy_rule=db_team.mercy_rule if hasattr(db_team, 'mercy_rule') else None,
                    mercy_count=db_team.mercy_count if hasattr(db_team, 'mercy_count') else 0,
                    assembled_items=db_team.assembled_items if hasattr(db_team, 'assembled_items') else None
                )
                
                # Load current task if task_id exists
                if db_team.current_task:
                    task = self._load_task_by_id(db_team.current_task)
                    if task:
                        team.current_task = task
                
                self.teams.append(team)
            
            logger.info(f"Loaded {len(self.teams)} teams for event {self.event_id}")
            return True
        except Exception as e:
            logger.error(f"Error loading teams: {e}")
            logger.error(traceback.format_exc())
            return False

    def get_points_awards(self, task_name: str) -> dict:
        """
        Get the points awards for a given task name
        """
        points_awards = {}
        try:
            tasks = self.tasks
            for task in tasks:
                if task.name == task_name:
                    for item in task.required_items:
                        print("Required item: ", item)
                        item: TaskItem = item
                        if item.name:
                            item_name = item.name
                            item_points = item.points
                            points_awards[item_name] = item_points
            return points_awards
        except Exception as e:
            logger.error(f"Error getting points awards: {e}")
            logger.error(traceback.format_exc())
            return None
        
    def create_team_embed(self, embed_type: str, team_obj: Team):
        print("Team object type: ", type(team_obj))
        components = []
        db_object: EventTeam = session.query(EventTeam).filter(EventTeam.event_id == self.event_id, EventTeam.name == team_obj.name).first()
        team_positions = {}
        for team in self.teams:
            team_positions[team.name] = team_obj.position
        teams_sorted = sorted(team_positions, key=lambda x: team_positions[x])
        team_rank = teams_sorted.index(team_obj.name) + 1
        embed = interactions.Embed(description=f"You are currently in `{add_ordinal_suffix(team_rank)}` place out of `{len(self.teams)}` teams.",color=0x00ff00)
        player_ranks = {}
        players: List[EventParticipant] = session.query(EventParticipant).filter(EventParticipant.team_id == team_obj.team_id,
                                                         EventParticipant.event_id == self.event_id).all()
        for player in players:  
            player: EventParticipant = player
            player_points = player.points
            player_ranks[player.player_id] = player_points
        top_player_raw = max(player_ranks, key=player_ranks.get)
        top_player: Player = session.query(Player).filter(Player.player_id == top_player_raw).first()
        top_player_points = player_ranks[top_player_raw]
        top_player_user = None
        if top_player:
            top_player_name = top_player.player_name
            if top_player.user_id:
                top_player_user = session.query(User).filter(User.user_id == top_player.user_id).first()
                if top_player_user:
                    top_player_name = top_player_user.username  
        if embed_type == "task":
            if team_obj.current_task:
                task = self._load_task_by_id(db_object.current_task)
                embed.add_field(name="Current Task", value=f"{task.name if task else 'No task assigned'}:\n{task.description if task else 'No description'}")
            else:
                embed.add_field(name="Current Task", value=f"Your task has been completed!\n**Press the button below to roll the dice!**",inline=False)
                components.append(interactions.Button(
                    style=interactions.ButtonStyle.BLUE,
                    label="Roll Dice",
                    custom_id="roll_dice",
                    emoji="game_die"
                ))
        embed.add_field(name=f"Team Statistics",
                        value=f"MVP: {top_player_name} ({top_player_points} pts)" + (f" (<@{top_player_user.discord_id}>)" if top_player_user else "\n") + 
                                        f"\nTasks completed: `{team_obj.turn_number - 1}`")

        embed.add_field(name=f"\n",value=f"Current Location: `#{db_object.current_location}` ({self.get_tile_emoji(tile_num=db_object.current_location)})\n" + 
                                f"Points earned: `{team_obj.points}`\n" + 
                                f"Gold: `{team_obj.gold}`")
        
        try:
            embed.add_field(name="Mercy Rule", value=f"<t:{int(team_obj.mercy_rule)}:R>" if team_obj.mercy_rule else "No mercy rule")
        except:
            pass
        if team_obj.active_effects:
            try:
                embed.add_field(name="Active Effects", value=f"{team_obj.active_effects}" if team_obj.active_effects else "No active effects")
            except:
                pass
        embed.set_thumbnail(url="https://www.droptracker.io/img/droptracker-small.gif")
        embed.set_author(name=f"{team_obj.name}")

        embed.set_footer(text=os.getenv("DISCORD_MESSAGE_FOOTER"))
        return embed, components

    def _load_task_by_id(self, task_id: int) -> Optional[Task]:
        """
        Load a task by its ID from the database
        
        Args:
            task_id: ID of the task to load
            
        Returns:
            Task object if found, None otherwise
        """
        try:
            db_task: EventTask = session.query(EventTask).get(task_id)
            if not db_task:
                return None
            
            # Convert required_items from string to appropriate format
            required_items = db_task.required_items
            if isinstance(required_items, str):
                try:
                    # Try to parse as JSON
                    required_items = json.loads(required_items)
                except json.JSONDecodeError:
                    # If it's not valid JSON, look up the task in our loaded tasks
                    for task in self.tasks:
                        if task.name == db_task.name:
                            required_items = task.required_items
                            break
                    else:
                        # If we can't find it, create a default
                        required_items = [{"item_id": required_items, "quantity": 1}]
            
            # Determine task type
            task_type = db_task.type if hasattr(db_task, 'type') and db_task.type else 'exact_item'
            
            # Create task object
            task = Task(
                task_id=db_task.id,
                name=db_task.name,
                description=db_task.description,
                required_items=required_items,
                points=db_task.points,
                is_assembly=db_task.is_assembly,
                type=task_type,
                difficulty=db_task.difficulty
            )
            
            # Add additional fields based on task type
            if task_type == 'point_collection' and hasattr(db_task, 'points'):
                task.points = db_task.points
            
            return task
        except Exception as e:
            logger.error(f"Error loading task by ID: {e}")
            logger.error(traceback.format_exc())
            return None
        

def add_ordinal_suffix(number: int) -> str:
    """
    Add ordinal suffix to a number
    """
    if 10 <= number % 100 <= 20:
        return f"{number}th"
    return f"{number}{['st', 'nd', 'rd'][number % 10 - 1]}"


def get_default_task_by_name(task_name: str) -> dict:
    """
    Get a default task by its name
    """
    try:
        for task in default_tasks:
            if task["name"] == task_name:
                return task
        return None
    except Exception as e:
        logger.error(f"Error getting default task by name: {e}")
        logger.error(traceback.format_exc())
        return None