from games.events.utils.event_config import EventConfig
import json
import logging
from typing import Any, Dict, Optional, Union, List
from db.eventmodels import EventConfigModel as EventConfigModel
from db.base import session

logger = logging.getLogger("boardgame.config")

class BoardGameConfig(EventConfig):
    """
    Configuration manager for board games
    
    Extends the base EventConfig with board game specific properties.
    """
    
    def create_default_config(self) -> bool:
        """
        Create default configuration for the board game
        
        Returns:
            True if successful, False otherwise
        """
        # First create base config
        if not super().create_default_config():
            return False
        
        # Then add board game specific config
        config_entries = [
            {
                "config_key": "game_state",
                "config_value": "default",
                "long_value": json.dumps({}),
                "update_number": 0
            },
            {
                "config_key": "team_category_id",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "game_board_channel_id",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "shop_channel_id",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "die_sides",
                "config_value": "6",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "number_of_dice",
                "config_value": "1",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "items_enabled",
                "config_value": "true",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "shop_enabled",
                "config_value": "true",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "board_size",
                "config_value": "142",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "starting_gold",
                "config_value": "5",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_assignment_method",
                "config_value": "manual",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_role_id_1",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_channel_id_1",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_role_id_2",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_channel_id_2",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_role_id_3",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_channel_id_3",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_role_id_4",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "team_channel_id_4",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "win_condition_points",
                "config_value": "100",
                "long_value": None,
                "update_number": 0
            }
        ]
        
        for entry in config_entries:
            config = EventConfigModel(
                event_id=self.event_id,
                **entry
            )
            session.add(config)
        
        try:
            session.commit()
            return True
        except Exception as e:
            logger.error(f"Error creating board game config: {e}")
            session.rollback()
            return False
    
    # Board game specific properties
    @property
    def team_category_id(self) -> str:
        """Team category ID for Discord channels"""
        return self._get_config("team_category_id", "0")
    
    @team_category_id.setter
    def team_category_id(self, value: str):
        self._set_config("team_category_id", value)
    
    @property
    def game_board_channel_id(self) -> str:
        """Game board channel ID"""
        return self._get_config("game_board_channel_id", "0")
    
    @game_board_channel_id.setter
    def game_board_channel_id(self, value: str):
        self._set_config("game_board_channel_id", value)
    
    @property
    def shop_channel_id(self) -> str:
        """Shop channel ID"""
        return self._get_config("shop_channel_id", "0")
    
    @shop_channel_id.setter
    def shop_channel_id(self, value: str):
        self._set_config("shop_channel_id", value)
    
    # Properties for team roles
    @property
    def team_assignment_method(self) -> str:
        """Method for assigning players to teams (manual, reaction_roles, etc.)"""
        return self._get_config("team_assignment_method", "manual")
    
    @team_assignment_method.setter
    def team_assignment_method(self, value: str):
        self._set_config("team_assignment_method", value)
    
    @property
    def team_channel_id_1(self) -> str:
        """Discord channel ID for team 1"""
        return self._get_config("team_channel_id_1", "0")
    
    @team_channel_id_1.setter
    def team_channel_id_1(self, value: str):
        self._set_config("team_channel_id_1", value)

    @property
    def team_role_id_1(self) -> str:
        """Discord role ID for team 1"""
        return self._get_config("team_role_id_1", "0")
    
    @team_role_id_1.setter
    def team_role_id_1(self, value: str):
        self._set_config("team_role_id_1", value)
    
    @property
    def team_role_id_2(self) -> str:
        """Discord role ID for team 2"""
        return self._get_config("team_role_id_2", "0")
    
    @team_role_id_2.setter
    def team_role_id_2(self, value: str):
        self._set_config("team_role_id_2", value)

    @property
    def team_channel_id_2(self) -> str:
        """Discord channel ID for team 2"""
        return self._get_config("team_channel_id_2", "0")
    
    @team_channel_id_2.setter
    def team_channel_id_2(self, value: str):
        self._set_config("team_channel_id_2", value)

    @property
    def team_role_id_3(self) -> str:
        """Discord role ID for team 3"""
        return self._get_config("team_role_id_3", "0")
    
    @team_role_id_3.setter
    def team_role_id_3(self, value: str):
        self._set_config("team_role_id_3", value)
    
    @property
    def team_channel_id_3(self) -> str:
        """Discord channel ID for team 3"""
        return self._get_config("team_channel_id_3", "0")
    
    @team_channel_id_3.setter
    def team_channel_id_3(self, value: str):
        self._set_config("team_channel_id_3", value)

    @property
    def team_role_id_4(self) -> str:
        """Discord role ID for team 4"""
        return self._get_config("team_role_id_4", "0")
    
    @team_role_id_4.setter
    def team_role_id_4(self, value: str):
        self._set_config("team_role_id_4", value)
    
    @property
    def team_channel_id_4(self) -> str:
        """Discord channel ID for team 4"""
        return self._get_config("team_channel_id_4", "0")
    
    @team_channel_id_4.setter
    def team_channel_id_4(self, value: str):
        self._set_config("team_channel_id_4", value)

    # Properties for game mechanics
    @property
    def die_sides(self) -> int:
        """Number of sides on the dice"""
        return int(self._get_config("die_sides", 6))
    
    @die_sides.setter
    def die_sides(self, value: int):
        self._set_config("die_sides", value)
    
    @property
    def number_of_dice(self) -> int:
        """Number of dice to roll"""
        return int(self._get_config("number_of_dice", 1))
    
    @number_of_dice.setter
    def number_of_dice(self, value: int):
        self._set_config("number_of_dice", value)
    
    @property
    def items_enabled(self) -> bool:
        """Whether items are enabled"""
        return self._get_config("items_enabled", "true").lower() == "true"
    
    @items_enabled.setter
    def items_enabled(self, value: bool):
        self._set_config("items_enabled", "true" if value else "false")
    
    @property
    def shop_enabled(self) -> bool:
        """Whether the shop is enabled"""
        return self._get_config("shop_enabled", "true").lower() == "true"
    
    @shop_enabled.setter
    def shop_enabled(self, value: bool):
        self._set_config("shop_enabled", "true" if value else "false")
    
    @property
    def win_condition_points(self) -> int:
        """Points needed to win"""
        return int(self._get_config("win_condition_points", 100))
    
    @win_condition_points.setter
    def win_condition_points(self, value: int):
        self._set_config("win_condition_points", value)
    
    @property
    def board_size(self) -> int:
        """Size of the game board"""
        return int(self._get_config("board_size", 142))
    
    @board_size.setter
    def board_size(self, value: int):
        self._set_config("board_size", value)
    
    @property
    def starting_gold(self) -> int:
        """Starting gold for teams"""
        return int(self._get_config("starting_gold", 5))
    
    @starting_gold.setter
    def starting_gold(self, value: int):
        self._set_config("starting_gold", value)
    
    @property
    def game_state(self) -> Dict[str, Any]:
        """Game state as a dictionary"""
        return self._get_long_config("game_state", {})
    
    @game_state.setter
    def game_state(self, value: Dict[str, Any]):
        self._set_long_config("game_state", value)
