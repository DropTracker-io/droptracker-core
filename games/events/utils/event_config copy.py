import json
from typing import Any, Dict, Optional, Union, List
from db.eventmodels import EventModel as EventModel, EventConfigModel as EventConfigModel, EventTeamModel, EventParticipant, EventShopItem, EventTeamInventory
from db.models import Player, User, Group
from db.base import session
import logging

logger = logging.getLogger("boardgame.config")

def create_default_config(event_id: int) -> bool:
    """
    Create a default configuration for a new event
    
    Args:
        event_id: ID of the event
        
    Returns:
        True if successful, False otherwise
    """
    config_entries = [
        {
            "config_key": "game_state",
            "config_value": "default",
            "long_value": json.dumps({}),
            "update_number": 0
        },
        {
            "config_key": "admin_channel_id",
            "config_value": "0",
            "long_value": None,
            "update_number": 0
        },
        {
            "config_key": "general_notification_channel_id",
            "config_value": "0",
            "long_value": None,
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
        }
    ]
    
    for entry in config_entries:
        config = EventConfigModel(
            event_id=event_id,
            **entry
        )
        session.add(config)
    
    try:
        session.commit()
        return True
    except Exception as e:
        logger.error(f"Error creating default config: {e}")
        session.rollback()
        return False

class EventConfig:
    """
    Configuration manager for board games
    
    Provides easy access to configuration options through properties.
    """
    
    def __init__(self, event_id: int):
        """
        Initialize the configuration manager
        
        Args:
            event_id: ID of the event
        """
        self.event_id = event_id
        self._config_cache: Dict[str, Dict[str, Any]] = {}
        self.load_config()
    
    def load_config(self) -> bool:
        """
        Load configuration from database
        
        Returns:
            True if successful, False otherwise
        """
        try:
            configs = session.query(EventConfigModel).filter(EventConfigModel.event_id == self.event_id).all()
            
            # If no configs exist, create default configuration
            if not configs:
                logger.info(f"No configuration found for event {self.event_id}, creating default")
                if not create_default_config(self.event_id):
                    return False
                configs = session.query(EventConfigModel).filter(EventConfigModel.event_id == self.event_id).all()
            
            # Cache configs
            self._config_cache = {}
            for config in configs:
                self._config_cache[config.config_key] = {
                    "value": config.config_value,
                    "long_value": config.long_value,
                    "update_number": config.update_number
                }
            
            return True
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            return False
    
    def _get_config(self, key: str, default: Any = None) -> str:
        """
        Get configuration value
        
        Args:
            key: Configuration key
            default: Default value if not found
            
        Returns:
            Configuration value
        """
        if key in self._config_cache:
            return self._config_cache[key]["value"]
        
        # If not in cache, try to load from database
        config = session.query(EventConfigModel).filter(
            EventConfigModel.event_id == self.event_id,
            EventConfigModel.config_key == key
        ).first()
        
        if config:
            # Update cache
            self._config_cache[key] = {
                "value": config.config_value,
                "long_value": config.long_value,
                "update_number": config.update_number
            }
            return config.config_value
        
        # If not found, create with default value
        if default is not None:
            default_str = str(default)
            config = EventConfigModel(
                event_id=self.event_id,
                config_key=key,
                config_value=default_str,
                long_value=None,
                update_number=0
            )
            session.add(config)
            try:
                session.commit()
                # Update cache
                self._config_cache[key] = {
                    "value": default_str,
                    "long_value": None,
                    "update_number": 0
                }
                return default_str
            except Exception as e:
                logger.error(f"Error creating config {key}: {e}")
                session.rollback()
        
        return str(default) if default is not None else ""
    
    def _get_long_config(self, key: str, default: Any = None) -> Any:
        """
        Get long configuration value (JSON)
        
        Args:
            key: Configuration key
            default: Default value if not found
            
        Returns:
            Parsed JSON value
        """
        if key in self._config_cache and self._config_cache[key]["long_value"]:
            try:
                return json.loads(self._config_cache[key]["long_value"])
            except json.JSONDecodeError:
                pass
        
        # If not in cache or invalid JSON, try to load from database
        config: EventConfigModel = session.query(EventConfigModel).filter(
            EventConfigModel.event_id == self.event_id,
            EventConfigModel.config_key == key
        ).first()
        
        if config and config.long_value:
            try:
                # Update cache
                self._config_cache[key] = {
                    "value": config.config_value,
                    "long_value": config.long_value,
                    "update_number": config.update_number
                }
                return json.loads(config.long_value)
            except json.JSONDecodeError:
                pass
        
        # If not found or invalid JSON, create with default value
        if default is not None:
            default_json = json.dumps(default)
            if config:
                config.long_value = default_json
            else:
                config = EventConfigModel(
                    event_id=self.event_id,
                    config_key=key,
                    config_value=str(default),
                    long_value=default_json,
                    update_number=0
                )
                session.add(config)
            
            try:
                session.commit()
                # Update cache
                self._config_cache[key] = {
                    "value": str(default),
                    "long_value": default_json,
                    "update_number": 0
                }
                return default
            except Exception as e:
                logger.error(f"Error creating long config {key}: {e}")
                session.rollback()
        
        return default
    
    def _set_config(self, key: str, value: Any) -> bool:
        """
        Set configuration value
        
        Args:
            key: Configuration key
            value: New value
            
        Returns:
            True if successful, False otherwise
        """
        value_str = str(value)
        
        # Check if config exists
        config = session.query(EventConfig).filter(
            EventConfig.event_id == self.event_id,
            EventConfig.config_key == key
        ).first()
        
        if config:
            # Update existing config
            config.config_value = value_str
            config.update_number += 1
        else:
            # Create new config
            config = EventConfigModel(
                event_id=self.event_id,
                config_key=key,
                config_value=value_str,
                long_value=None,
                update_number=0
            )
            session.add(config)
        
        try:
            session.commit()
            # Update cache
            if key in self._config_cache:
                self._config_cache[key]["value"] = value_str
                self._config_cache[key]["update_number"] = config.update_number
            else:
                self._config_cache[key] = {
                    "value": value_str,
                    "long_value": None,
                    "update_number": config.update_number
                }
            return True
        except Exception as e:
            logger.error(f"Error setting config {key}: {e}")
            session.rollback()
            return False
    
    def _set_long_config(self, key: str, value: Any) -> bool:
        """
        Set long configuration value (JSON)
        
        Args:
            key: Configuration key
            value: New value (will be JSON encoded)
            
        Returns:
            True if successful, False otherwise
        """
        value_str = str(value)
        value_json = json.dumps(value)
        
        # Check if config exists
        config = session.query(EventConfigModel).filter(
            EventConfigModel.event_id == self.event_id,
            EventConfigModel.config_key == key
        ).first()
        
        if config:
            # Update existing config
            config.config_value = value_str
            config.long_value = value_json
            config.update_number += 1
        else:
            # Create new config
            config = EventConfigModel(
                event_id=self.event_id,
                config_key=key,
                config_value=value_str,
                long_value=value_json,
                update_number=0
            )
            session.add(config)
        
        try:
            session.commit()
            # Update cache
            if key in self._config_cache:
                self._config_cache[key]["value"] = value_str
                self._config_cache[key]["long_value"] = value_json
                self._config_cache[key]["update_number"] = config.update_number
            else:
                self._config_cache[key] = {
                    "value": value_str,
                    "long_value": value_json,
                    "update_number": config.update_number
                }
            return True
        except Exception as e:
            logger.error(f"Error setting long config {key}: {e}")
            session.rollback()
            return False
    
    # Properties for Discord channel IDs
    @property
    def admin_channel_id(self) -> int:
        """Admin channel ID"""
        return int(self._get_config("admin_channel_id", 0))
    
    @admin_channel_id.setter
    def admin_channel_id(self, value: int):
        self._set_config("admin_channel_id", value)
    
    @property
    def general_notification_channel_id(self) -> int:
        """General notification channel ID"""
        return int(self._get_config("general_notification_channel_id", 0))
    
    @general_notification_channel_id.setter
    def general_notification_channel_id(self, value: int):
        self._set_config("general_notification_channel_id", value)
    
    @property
    def team_category_id(self) -> int:
        """Team category ID for Discord channels"""
        return int(self._get_config("team_category_id", 0))
    
    @team_category_id.setter
    def team_category_id(self, value: int):
        self._set_config("team_category_id", value)
    
    @property
    def game_board_channel_id(self) -> int:
        """Game board channel ID"""
        return int(self._get_config("game_board_channel_id", 0))
    
    @game_board_channel_id.setter
    def game_board_channel_id(self, value: int):
        self._set_config("game_board_channel_id", value)
    
    @property
    def shop_channel_id(self) -> int:
        """Shop channel ID"""
        return int(self._get_config("shop_channel_id", 0))
    
    @shop_channel_id.setter
    def shop_channel_id(self, value: int):
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
    def team_channel_id_1(self) -> int:
        """Discord channel ID for team 1"""
        return int(self._get_config("team_channel_id_1", 0))
    
    @team_channel_id_1.setter
    def team_channel_id_1(self, value: int):
        self._set_config("team_channel_id_1", value)

    @property
    def team_role_id_1(self) -> int:
        """Discord role ID for team 1"""
        return int(self._get_config("team_role_id_1", 0))
    
    @team_role_id_1.setter
    def team_role_id_1(self, value: int):
        self._set_config("team_role_id_1", value)
    
    @property
    def team_role_id_2(self) -> int:
        """Discord role ID for team 2"""
        return int(self._get_config("team_role_id_2", 0))
    
    @team_role_id_2.setter
    def team_role_id_2(self, value: int):
        self._set_config("team_role_id_2", value)

    @property
    def team_channel_id_2(self) -> int:
        """Discord channel ID for team 2"""
        return int(self._get_config("team_channel_id_2", 0))
    
    @team_channel_id_2.setter
    def team_channel_id_2(self, value: int):
        self._set_config("team_channel_id_2", value)

    @property
    def team_role_id_3(self) -> int:
        """Discord role ID for team 3"""
        return int(self._get_config("team_role_id_3", 0))
    
    @team_role_id_3.setter
    def team_role_id_3(self, value: int):
        self._set_config("team_role_id_3", value)
    
    @property
    def team_channel_id_3(self) -> int:
        """Discord channel ID for team 3"""
        return int(self._get_config("team_channel_id_3", 0))
    
    @team_channel_id_3.setter
    def team_channel_id_3(self, value: int):
        self._set_config("team_channel_id_3", value)

    @property
    def team_role_id_4(self) -> int:
        """Discord role ID for team 4"""
        return int(self._get_config("team_role_id_4", 0))
    
    @team_role_id_4.setter
    def team_role_id_4(self, value: int):
        self._set_config("team_role_id_4", value)
    
    @property
    def team_channel_id_4(self) -> int:
        """Discord channel ID for team 4"""
        return int(self._get_config("team_channel_id_4", 0))
    
    @team_channel_id_4.setter
    def team_channel_id_4(self, value: int):
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
        return int(self._get_config("board_size", 30))
    
    @board_size.setter
    def board_size(self, value: int):
        self._set_config("board_size", value)
    
    @property
    def starting_gold(self) -> int:
        """Starting gold for teams"""
        return int(self._get_config("starting_gold", 100))
    
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
    
    # Dynamic property access for any config
    def __getattr__(self, name: str) -> Any:
        """
        Get any configuration value by attribute name
        
        Args:
            name: Configuration key
            
        Returns:
            Configuration value
            
        Raises:
            AttributeError: If configuration key not found
        """
        if name in self._config_cache:
            # Try to determine the type
            value = self._config_cache[name]["value"]
            
            # Try to convert to appropriate type
            if value.isdigit():
                return int(value)
            elif value.lower() in ("true", "false"):
                return value.lower() == "true"
            else:
                try:
                    return float(value)
                except ValueError:
                    return value
        
        # If we get here, the config doesn't exist
        # Create it with a default empty value
        logger.warning(f"Accessing non-existent config '{name}', creating with empty value")
        self._set_config(name, "")
        return ""
    
    def __setattr__(self, name: str, value: Any):
        """
        Set any configuration value by attribute name
        
        Args:
            name: Configuration key
            value: New value
        """
        # Don't intercept internal attributes
        if name in ("event_id", "_config_cache"):
            super().__setattr__(name, value)
            return
        
        # Set config value
        self._set_config(name, value)