
import random
from typing import List, Dict, Optional, Tuple, Union
from dataclasses import dataclass, field, asdict
from db.models import Player, session, Group
from utils.redis import redis_client
import json
from enum import Enum
import interactions
from interactions import Embed


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
    gold: int = 0
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

class GielinorRaceEmbed:
    @staticmethod
    def task_completed(team: Team, task: Task, points_earned: int, items_received: List[Item]) -> interactions.Embed:
        embed = interactions.Embed(title=f"Task Completed by {team.name}", color=0x00ff00)
        embed.add_field(name="Task", value=task.name, inline=False)
        embed.add_field(name="Points Earned", value=str(points_earned), inline=True)
        embed.add_field(name="New Total Points", value=str(team.points), inline=True)
        embed.add_field(name="New Position", value=str(team.position), inline=True)
        
        if items_received:
            items_str = ", ".join([f"{item.emoji} {item.name}" for item in items_received])
            embed.add_field(name="Items Received", value=items_str, inline=False)
        
        return embed

    @staticmethod
    def player_joined_team(player: Player, team: Team) -> interactions.Embed:
        embed = interactions.Embed(title=f"Player Joined Team", color=0x0000ff)
        embed.add_field(name="Player", value=player.player_name, inline=True)
        embed.add_field(name="Team", value=team.name, inline=True)
        return embed

    @staticmethod
    def team_won(team: Team) -> interactions.Embed:
        embed = interactions.Embed(title="Gielinor Race Winner!", color=0xffd700)
        embed.add_field(name="Winning Team", value=team.name, inline=False)
        embed.add_field(name="Final Points", value=str(team.points), inline=True)
        embed.add_field(name="Players", value=", ".join([player.player_name for player in team.players]), inline=False)
        return embed

    @staticmethod
    def item_used(team: Team, item: Item, effect: str) -> interactions.Embed:
        embed = interactions.Embed(title=f"Item Used by {team.name}", color=0xff00ff)
        embed.add_field(name="Item", value=f"{item.emoji} {item.name}", inline=True)
        embed.add_field(name="Effect", value=effect, inline=False)
        return embed

    @staticmethod
    def game_status(game: 'GielinorRace') -> interactions.Embed:
        embed = interactions.Embed(title="Gielinor Race Status", color=0x00ffff)
        for team in game.teams:
            embed.add_field(name=f"Team: {team.name}", value=f"Position: {team.position}\nPoints: {team.points}", inline=False)
        return embed
    
    @staticmethod
    def embed_base(game: 'GielinorRace') -> interactions.Embed:
        embed = interactions.Embed(title="Gielinor Race", color=0x00ffff)
        embed.set_footer(text=f"Powered by the DropTracker | https://www.droptracker.io/")
        return embed
    
    @staticmethod
    def embed_base_with_title(game: 'GielinorRace', title: str) -> interactions.Embed:
        embed = GielinorRaceEmbed.embed_base(game)
        embed.title = title


class GielinorRace:
    def __init__(self, bot: interactions.Client, group_id: int, board_size: int = 100, 
                 join_channel: int = None, shop_channel: int = None, 
                 noti_channel: int = None, admin_channel: int = None):
        """
        Initialize a new Gielinor Race game for a specific group.

        :param group_id: The ID of the group playing the game
        :param board_size: The number of tiles on the game board (50-250)
        :param channel_id: The ID of the Discord channel to send game updates to
        """
        self.group_id = group_id
        self.board_size = 100
        self.teams: List[Team] = []
        self.tiles: List[Tile] = []
        for i, tile_number in enumerate(range(self.board_size)):
            if tile_number % 25 == 0:
                self.tiles.append(Tile(TileType.AIR, tile_number))
            elif tile_number % 50 == 0:
                self.tiles.append(Tile(TileType.EARTH, tile_number))
            elif tile_number % 75 == 0:
                self.tiles.append(Tile(TileType.FIRE, tile_number))
            else:
                self.tiles.append(Tile(TileType.WATER, tile_number))

        self.shop: Dict[str, Item] = {
            "teleport": Item("Teleport", 50, "Instantly re-roll your current task to one difficulty tier lower.", "🌀", ItemType.SPECIAL, 3),
            "protection": Item("Protection", 30, "Your next die roll will have +2 added to it.", ":game_die:", ItemType.SPECIAL, 2),
            "boost": Item("Boost", 40, "Doubles the number of coins you receive for completing your next task.", "⚡", ItemType.SPECIAL, 2)
        }

        self.tasks: Dict[TileType, List[Task]] = {tile_type: [] for tile_type in TileType}
        self.task_file_path = 'games/events/task_store/default.json'
        self.load_tasks()
        self.load_game_state()
        self.current_team_index = 0
        self.notification_channel_id = noti_channel
        self.join_channel_id = join_channel
        self.shop_channel_id = shop_channel


    def load_tasks(self):
        """
        Load tasks from a JSON file and populate the tasks list.
        """
        with open(self.task_file_path, 'r') as f:
            task_data = json.load(f)
            for task in task_data:
                tile_type = TileType(task['difficulty'])
                required_items = task.get('required_items', task['name'])
                is_assembly = task.get('is_assembly', False)
                
                if isinstance(required_items, list) and not is_assembly:
                    required_items = [TaskItem(item['name'], item.get('points', 0)) for item in required_items]
                elif isinstance(required_items, str):
                    required_items = TaskItem(required_items)
                
                self.tasks[tile_type].append(Task(
                    task['name'],
                    tile_type,
                    task['points'],
                    required_items,
                    is_assembly
                ))

    def save_game_state(self):
        """
        Save the current game state to Redis.
        """
        game_state = {
            "board_size": self.board_size,
            "teams": [
                {
                    "name": team.name,
                    "players": [player.player_id for player in team.players],
                    "position": team.position,
                    "points": team.points,
                    "inventory": [item.name for item in team.inventory],
                    "cooldowns": team.cooldowns,
                    "active_effects": team.active_effects
                } for team in self.teams
            ]
        }
        redis_client.set(f"gielinor_race:{self.group_id}", json.dumps(game_state))

    def load_game_state(self):
        """
        Load the game state from Redis, if it exists.
        """
        game_state = redis_client.get(f"gielinor_race:{self.group_id}")
        if game_state:
            game_state = json.loads(game_state)
            self.board_size = game_state["board_size"]
            self.tiles = self._generate_tiles()
            self.teams = []
            for team_data in game_state["teams"]:
                players = [session.query(Player).get(player_id) for player_id in team_data["players"]]
                team = Team(
                    name=team_data["name"],
                    players=players,
                    position=team_data["position"],
                    points=team_data["points"],
                    inventory=[self.shop[item_name] for item_name in team_data["inventory"]],
                    cooldowns=team_data["cooldowns"],
                    active_effects=team_data["active_effects"]
                )
                self.teams.append(team)

    def roll_dice(self) -> int:
        """
        Roll a 6-sided die.

        :return: A random number between 1 and 6
        """
        return random.randint(1, 6)

    def move_team(self, team_name: str, spaces: int) -> Tile:
        """
        Move a team forward on the board and return the tile they land on.

        :param team_name: The name of the team to move
        :param spaces: The number of spaces to move forward
        :return: The Tile object the team landed on
        """
        team = self.get_team(team_name)
        team.position = min(team.position + spaces, self.board_size - 1)
        self.save_game_state()
        return self.tiles[team.position]

    def generate_task(self, tile_type: TileType) -> Task:
        """
        Generate a random task from the available tasks for the given tile type.

        :param tile_type: The TileType to generate a task for
        :return: A randomly selected Task object
        """
        return random.choice(self.tasks[tile_type])

    def handle_roll_and_move(self, team_name: str) -> Tuple[int, Tile, Task]:
        """
        Handle a team's roll, move, and task assignment.

        :param team_name: The name of the team rolling and moving
        :return: A tuple containing (roll_result, landed_tile, assigned_task)
        """
        roll_result = self.roll_dice()
        team = self.get_team(team_name)
        current_loc = team.position
        landed_tile = self.move_team(team_name, roll_result)
        assigned_task = self.generate_task(landed_tile.type)
        logger.info(f"Team {team_name} rolled a {roll_result} and moved from {current_loc} to {landed_tile.position}. Assigned task: {assigned_task.name}")
        return roll_result, landed_tile, assigned_task

    def add_team(self, name: str, player_ids: List[int]) -> None:
        """
        Add a new team to the game.

        :param name: The name of the team
        :param player_ids: List of player IDs to add to the team
        """
        players = [session.query(Player).get(player_id) for player_id in player_ids]
        self.teams.append(Team(name, players))
        self.save_game_state()
        logger.info(f"Team {name} added with players: {', '.join([player.player_name for player in players])}")

    def award_points(self, team_name: str, task_difficulty: int) -> None:
        """
        Award points to a team for completing an entire task, based on the task difficulty.

        :param team_name: The name of the team to award points to
        :param task_difficulty: The difficulty of the completed task
        """
        team = self.get_team(team_name)
        points = task_difficulty * 10
        team.points += points
        self.move_team(team_name, task_difficulty)
        self.save_game_state()
        logger.info(f"Team {team_name} earned {points} points for task difficulty {task_difficulty}. New total points: {team.points}")

    def get_team(self, team_name: str) -> Team:
        """
        Get a team object by its name.

        :param team_name: The name of the team to retrieve
        :return: The Team object
        """
        return next(team for team in self.teams if team.name == team_name)

    def purchase_item(self, team_name: str, item_name: str) -> bool:
        """
        Attempt to purchase an item for a team.

        :param team_name: The name of the team making the purchase
        :param item_name: The name of the item to purchase
        :return: True if the purchase was successful, False otherwise
        """
        team = self.get_team(team_name)
        item = self.shop.get(item_name)
        if item and team.points >= item.cost:
            team.points -= item.cost
            team.inventory.append(item)
            self.save_game_state()
            return True
        return False

    def use_item(self, team_name: str, item_name: str) -> Tuple[bool, str, Item]:
        """
        Attempt to use an item for a team.

        :param team_name: The name of the team using the item
        :param item_name: The name of the item to use
        :return: A tuple containing (success, effect_message, item_used)
        """
        team = self.get_team(team_name)
        item = next((item for item in team.inventory if item.name == item_name), None)
        if item and team.cooldowns.get(item.name, 0) == 0:
            team.inventory.remove(item)
            team.cooldowns[item.name] = item.cooldown
            effect = self._apply_item_effect(team, item)
            self.save_game_state()
            return True, effect, item
        return False, f"{team_name} can't use {item_name} at this time.", None

    def _apply_item_effect(self, team: Team, item: Item) -> str:
        """
        Apply the effect of an item to a team.

        :param team: The team using the item
        :param item: The item being used
        :return: A string describing the effect of the item
        """
        if item.name == "Teleport":
            spaces = random.randint(1, 6)
            self.move_team(team.name, spaces)
            return f"{team.name} used Teleport and moved {spaces} spaces forward!"
        elif item.name == "Protection":
            # Implement protection logic
            return f"{team.name} is protected from the next negative effect!"
        elif item.name == "Boost":
            # Implement boost logic
            return f"{team.name} will receive double points for the next task!"
        return f"Unknown item effect for {item.name}"

    def complete_task(self, team_name: str, task: Task) -> Tuple[int, List[Item]]:
        """
        Attempt to complete a task for a team.

        :param team_name: The name of the team completing the task
        :param task: The Task object to complete
        :return: A tuple containing (points_earned, items_received)
        """
        team = self.get_team(team_name)
        points_earned = 0
        items_received = []

        if isinstance(task.required_items, TaskItem):
            # Single item task
            if self._has_required_item(team, task.required_items.name):
                points_earned = task.points
                self._remove_item(team, task.required_items.name)
        elif task.is_assembly:
            # Assembly task
            if self._has_all_required_items(team, [item.name for item in task.required_items]):
                points_earned = task.points
                for item in task.required_items:
                    self._remove_item(team, item.name)
        else:
            # Point accumulation task
            for required_item in task.required_items:
                while self._has_required_item(team, required_item.name) and points_earned < task.points:
                    points_earned += required_item.points
                    self._remove_item(team, required_item.name)
                    if points_earned >= task.points:
                        break

        if points_earned >= task.points:
            team.points += task.points
            self.move_team(team_name, task.difficulty.value)

            # Chance to receive items
            if random.random() < 0.5:  # 50% chance to receive an item
                received_item = random.choice(list(self.shop.values()))
                team.inventory.append(received_item)
                items_received.append(received_item)

        self.save_game_state()
        return points_earned, items_received

    def _has_required_item(self, team: Team, item_name: str) -> bool:
        """
        Check if a team has a required item in their inventory.

        :param team: The Team object to check
        :param item_name: The name of the required item
        :return: True if the team has the item, False otherwise
        """
        return any(item.name == item_name for item in team.inventory)

    def _has_all_required_items(self, team: Team, item_names: List[str]) -> bool:
        """
        Check if a team has all required items for an assembly task.

        :param team: The Team object to check
        :param item_names: List of required item names
        :return: True if the team has all items, False otherwise
        """
        team_items = set(item.name for item in team.inventory)
        required_items = set(item_names)
        return required_items.issubset(team_items)

    def _remove_item(self, team: Team, item_name: str) -> None:
        """
        Remove an item from a team's inventory.

        :param team: The Team object to remove the item from
        :param item_name: The name of the item to remove
        """
        for item in team.inventory:
            if item.name == item_name:
                team.inventory.remove(item)
                break

    def update_cooldowns(self) -> None:
        """
        Update cooldowns for all teams' items.
        """
        for team in self.teams:
            for item, cooldown in team.cooldowns.items():
                if cooldown > 0:
                    team.cooldowns[item] -= 1
        self.save_game_state()

    def generate_map(self) -> str:
        """
        Generate a string representation of the game map.

        :return: A string representing the current game map
        """
        # Placeholder for map generation logic
        return "Map representation of team positions"

    def reset_game(self) -> None:
        """
        Reset the game state for all teams.
        """
        for team in self.teams:
            team.position = 0
            team.points = 0
            team.inventory.clear()
            team.cooldowns.clear()
        self.save_game_state()

    def set_board_size(self, size: int) -> None:
        """
        Set the size of the game board.

        :param size: The new size of the board
        """
        self.board_size = max(50, min(250, size))
        self.tiles = self._generate_tiles()
        self.save_game_state()

    def add_shop_item(self, name: str, cost: int, effect: str, emoji: str, item_type: ItemType, cooldown: int) -> None:
        """
        Add a new item to the shop.

        :param name: The name of the item
        :param cost: The cost of the item
        :param effect: The effect description of the item
        :param emoji: The emoji representation of the item
        :param item_type: The type of the item (OFFENSIVE, DEFENSIVE, or SPECIAL)
        :param cooldown: The cooldown period of the item
        """
        self.shop[name] = Item(name, cost, effect, emoji, item_type, cooldown)

    def remove_shop_item(self, name: str) -> None:
        """
        Remove an item from the shop.

        :param name: The name of the item to remove
        """
        self.shop.pop(name, None)

    def set_team_points(self, team_name: str, points: int) -> None:
        """
        Set the points for a specific team.

        :param team_name: The name of the team
        :param points: The new point value for the team
        """
        team = self.get_team(team_name)
        team.points = points
        self.save_game_state()

    def set_team_position(self, team_name: str, position: int) -> None:
        """
        Set the position for a specific team.

        :param team_name: The name of the team
        :param position: The new position for the team
        """
        team = self.get_team(team_name)
        team.position = min(position, self.board_size)
        self.save_game_state()

    def check_winner(self) -> Optional[str]:
        """
        Check if there's a winner in the game.

        :return: The name of the winning team, or None if there's no winner yet
        """
        for team in self.teams:
            if team.position >= self.board_size:
                return f"{team.name} has won the Gielinor Race!"
        return None

    def handle_join_team(self, player: Player, team_name: str) -> interactions.Embed:
        """
        Handle a player joining a team.

        :param player: The Player object joining the team
        :param team_name: The name of the team to join
        :return: An Embed object with the result of the action
        """
        team = self.get_team(team_name)
        if player not in team.players:
            team.players.append(player)
            self.save_game_state()
            return GielinorRaceEmbed.player_joined_team(player, team)
        return interactions.Embed(title="Error", description="Player is already in the team", color=0xff0000)

    def handle_complete_task(self, team_name: str) -> interactions.Embed:
        """
        Handle a team completing a task.

        :param team_name: The name of the team completing the task
        :return: An Embed object with the result of the action
        """
        task = self.generate_task()
        points_earned, items_received = self.complete_task(team_name, task)
        if points_earned > 0:
            team = self.get_team(team_name)
            return GielinorRaceEmbed.task_completed(team, task, points_earned, items_received)
        return interactions.Embed(title="Error", description="Task could not be completed", color=0xff0000)

    def handle_use_item(self, team_name: str, item_name: str) -> interactions.Embed:
        """
        Handle a team using an item.

        :param team_name: The name of the team using the item
        :param item_name: The name of the item being used
        :return: An Embed object with the result of the action
        """
        success, effect, item = self.use_item(team_name, item_name)
        if success:
            team = self.get_team(team_name)
            return GielinorRaceEmbed.item_used(team, item, effect)
        return interactions.Embed(title="Error", description=effect, color=0xff0000)

    def handle_game_status(self) -> interactions.Embed:
        """
        Handle a request for the current game status.

        :return: An Embed object with the current game status
        """
        return GielinorRaceEmbed.game_status(self)

    def handle_check_winner(self) -> Optional[interactions.Embed]:
        """
        Handle a request to check for a winner.

        :return: An Embed object with the winner information, or None if there's no winner yet
        """
        winner = self.check_winner()
        if winner:
            team = self.get_team(winner.split(" has won")[0])
            return GielinorRaceEmbed.team_won(team)
        return None

    def handle_roll_and_move(self, team_name: str) -> interactions.Embed:
        """
        Handle a team's roll, move, and task assignment, and create an Embed for the result.

        :param team_name: The name of the team rolling and moving
        :return: An Embed object with the result of the action
        """
        roll_result, landed_tile, assigned_task = self.handle_roll_and_move(team_name)
        team = self.get_team(team_name)
        
        embed = interactions.Embed(title=f"{team_name}'s Turn", color=0x00ff00)
        embed.add_field(name="Roll Result", value=str(roll_result), inline=True)
        embed.add_field(name="New Position", value=str(team.position), inline=True)
        embed.add_field(name="Tile Type", value=landed_tile.type.value.capitalize(), inline=True)
        embed.add_field(name="Assigned Task", value=assigned_task.name, inline=False)
        embed.add_field(name="Task Difficulty", value=assigned_task.difficulty.value.capitalize(), inline=True)
        embed.add_field(name="Task Points", value=str(assigned_task.points), inline=True)
        
        return embed

    def get_game_state(self) -> Dict:
        """
        Assemble all necessary information for display on a webpage.

        :return: A dictionary containing the current game state
        """
        return {
            "group_id": self.group_id,
            "board_size": self.board_size,
            "teams": [
                {
                    "name": team.name,
                    "position": team.position,
                    "points": team.points,
                    "players": [
                        {
                            "id": player.player_id,
                            "name": player.player_name
                        } for player in team.players
                    ],
                    "inventory": [asdict(item) for item in team.inventory],
                    "cooldowns": team.cooldowns,
                    "active_effects": team.active_effects
                } for team in self.teams
            ],
            "shop": {name: asdict(item) for name, item in self.shop.items()},
            "current_tile_types": [self.tiles[team.position].type.value for team in self.teams]
        }

    # Administrative functions
    def add_team(self, name: str, player_ids: List[int]) -> None:
        """
        Add a new team to the game.

        :param name: The name of the team
        :param player_ids: List of player IDs to add to the team
        """
        players = [session.query(Player).get(player_id) for player_id in player_ids]
        self.teams.append(Team(name, players))
        self.save_game_state()

    def remove_team(self, name: str) -> None:
        """
        Remove a team from the game.

        :param name: The name of the team to remove
        """
        self.teams = [team for team in self.teams if team.name != name]
        self.save_game_state()

    def add_player_to_team(self, team_name: str, player_id: int) -> None:
        """
        Add a player to an existing team.

        :param team_name: The name of the team to add the player to
        :param player_id: The ID of the player to add
        """
        team = self.get_team(team_name)
        player = session.query(Player).get(player_id)
        if player not in team.players:
            team.players.append(player)
            self.save_game_state()

    def remove_player_from_team(self, team_name: str, player_id: int) -> None:
        """
        Remove a player from a team.

        :param team_name: The name of the team to remove the player from
        :param player_id: The ID of the player to remove
        """
        team = self.get_team(team_name)
        team.players = [player for player in team.players if player.player_id != player_id]
        self.save_game_state()

    def set_board_size(self, size: int) -> None:
        """
        Set the size of the game board.

        :param size: The new size of the board
        """
        self.board_size = max(50, min(250, size))
        self.tiles = self._generate_tiles()
        self.save_game_state()

    def add_shop_item(self, name: str, cost: int, effect: str, emoji: str, item_type: ItemType, cooldown: int) -> None:
        """
        Add a new item to the shop.

        :param name: The name of the item
        :param cost: The cost of the item
        :param effect: The effect description of the item
        :param emoji: The emoji representation of the item
        :param item_type: The type of the item (OFFENSIVE, DEFENSIVE, or SPECIAL)
        :param cooldown: The cooldown period of the item
        """
        self.shop[name] = Item(name, cost, effect, emoji, item_type, cooldown)

    def remove_shop_item(self, name: str) -> None:
        """
        Remove an item from the shop.

        :param name: The name of the item to remove
        """
        self.shop.pop(name, None)

    def set_team_points(self, team_name: str, points: int) -> None:
        """
        Set the points for a specific team.

        :param team_name: The name of the team
        :param points: The new point value for the team
        """
        team = self.get_team(team_name)
        team.points = points
        self.save_game_state()

    def set_team_position(self, team_name: str, position: int) -> None:
        """
        Set the position for a specific team.

        :param team_name: The name of the team
        :param position: The new position for the team
        """
        team = self.get_team(team_name)
        team.position = min(position, self.board_size)
        self.save_game_state()

    def roll_and_move(self, team_name: str) -> Tuple[int, Tile, Task]:
        team = self.get_team(team_name)
        roll = self.roll_dice()
        
        # Check for effects that modify the roll
        for other_team in self.teams:
            if other_team.name != team_name:
                for effect, turns in other_team.active_effects.items():
                    if effect == "lower_roll" and turns > 0:
                        roll = max(1, roll - 2)  # Reduce roll by 2, minimum 1
                        other_team.active_effects[effect] -= 1
        
        # Move the team
        spaces_to_move = roll
        for effect, turns in team.active_effects.items():
            if effect == "move_backwards" and turns > 0:
                spaces_to_move = -spaces_to_move
                team.active_effects[effect] -= 1
        
        new_tile = self.move_team(team_name, spaces_to_move)
        task = self.generate_task(new_tile.type)
        
        self.send_discord_message(f"{team_name} rolled a {roll} and moved to {new_tile.type.value} tile at position {new_tile.position}.")
        return roll, new_tile, task

    def assign_task(self, team_name: str, tile: Tile) -> Task:
        task = self.generate_task(tile.type)
        team = self.get_team(team_name)
        team.current_task = task
        self.send_discord_message(f"{team_name} has been assigned the task: {task.name}")
        return task

    def check_task_completion(self, team_name: str, item_name: str) -> Tuple[bool, int, List[Item]]:
        team = self.get_team(team_name)
        task = team.current_task
        if not task:
            return False, 0, []

        if isinstance(task.required_items, TaskItem):
            if item_name == task.required_items.name:
                return self.complete_task(team_name, task)
        elif task.is_assembly:
            team.assembled_items.append(item_name)
            if set(team.assembled_items) == set(item.name for item in task.required_items):
                return self.complete_task(team_name, task)
        else:
            for required_item in task.required_items:
                if item_name == required_item.name:
                    team.task_progress += required_item.points
                    if team.task_progress >= task.points:
                        return self.complete_task(team_name, task)
                    break

        return False, 0, []

    def use_item(self, team_name: str, item_name: str) -> Tuple[bool, str, Item]:
        success, effect, item = super().use_item(team_name, item_name)
        if success:
            self.send_discord_message(f"{team_name} used {item_name}. Effect: {effect}")
        return success, effect, item

    def apply_item_effects(self, team_name: str, event_type: str) -> None:
        team = self.get_team(team_name)
        for other_team in self.teams:
            if other_team.name != team_name:
                for item in other_team.inventory:
                    if item.item_type == ItemType.OFFENSIVE and event_type == "roll":
                        if random.random() < 0.3:  # 30% chance to activate
                            effect = "lower_roll" if random.random() < 0.5 else "move_backwards"
                            team.active_effects[effect] = 1
                            self.send_discord_message(f"{other_team.name}'s {item.name} activated against {team_name}!")

    def next_turn(self) -> None:
        self.current_team_index = (self.current_team_index + 1) % len(self.teams)
        current_team = self.teams[self.current_team_index]
        
        # Decrease cooldowns
        for item, cooldown in current_team.cooldowns.items():
            if cooldown > 0:
                current_team.cooldowns[item] -= 1
        
        # Apply turn-based effects
        for effect in list(current_team.active_effects.keys()):
            current_team.active_effects[effect] -= 1
            if current_team.active_effects[effect] <= 0:
                del current_team.active_effects[effect]
        
        self.send_discord_message(f"It's now {current_team.name}'s turn!")

    def send_discord_message(self, message: str) -> None:
        if self.notification_channel_id:
            try:
                bot: interactions.Client = self.bot
                channel: interactions.Channel = bot.fetch_channel(self.notification_channel_id)
                channel.send(message)
            except Exception as e:
                logger.error(f"Error sending message to Discord: {e}")

    def check_win_condition(self) -> Optional[Team]:
        for team in self.teams:
            if team.position >= self.board_size - 1:
                self.send_discord_message(f"{team.name} has won the Gielinor Race!")
                return team
        return None

    def game_loop(self) -> None:
        while True:
            current_team = self.teams[self.current_team_index]
            self.apply_item_effects(current_team.name, "roll")
            roll, tile, task = self.roll_and_move(current_team.name)
            self.assign_task(current_team.name, tile)
            
            # Wait for task completion (this would be handled by user input in the actual implementation)
            # For now, we'll simulate it with a random completion
            if random.random() < 0.7:  # 70% chance to complete the task
                success, points, items = self.check_task_completion(current_team.name, task.required_items[0].name if isinstance(task.required_items, list) else task.required_items.name)
                if success:
                    self.send_discord_message(f"{current_team.name} completed the task and earned {points} points!")
            
            winner = self.check_win_condition()
            if winner:
                break
            
            self.next_turn()
            self.save_game_state()