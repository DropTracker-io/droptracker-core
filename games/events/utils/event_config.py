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
            "config_value": "[1,2,3,4,5]",
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
    Base configuration manager for events
    
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
                if not self.create_default_config():
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
    
    def create_default_config(self) -> bool:
        """
        Create default configuration for the event
        
        Returns:
            True if successful, False otherwise
        """
        config_entries = [
            {
                "config_key": "general_notification_channel_id",
                "config_value": "0",
                "long_value": None,
                "update_number": 0
            },
            {
                "config_key": "admin_notification_channel_id",
                "config_value": "0",
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
            logger.error(f"Error creating default config: {e}")
            session.rollback()
            return False
    
    def _get_config(self, key: str, default: Any = None) -> str:
        """
        Get a configuration value
        
        Args:
            key: Configuration key
            default: Default value if not found
            
        Returns:
            Configuration value
        """
        if key in self._config_cache:
            return self._config_cache[key]["value"]
        config_entry = session.query(EventConfigModel).filter(EventConfigModel.event_id == self.event_id, EventConfigModel.config_key == key).first()
        if config_entry:
            return config_entry.config_value
        return str(default) if default is not None else ""
    
    def _get_long_config(self, key: str, default: Any = None) -> Any:
        """
        Get a long configuration value (JSON)
        
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
                logger.error(f"Error parsing JSON for config {key}")
                return default
        config_entry = session.query(EventConfigModel).filter(EventConfigModel.event_id == self.event_id, EventConfigModel.config_key == key).first()
        if config_entry:
            return json.loads(config_entry.long_value)
        return default
    
    def _set_config(self, key: str, value: Any) -> bool:
        """
        Set a configuration value
        
        Args:
            key: Configuration key
            value: Value to set
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Convert value to string
            str_value = str(value)
            
            # Check if config exists
            config = session.query(EventConfigModel).filter(
                EventConfigModel.event_id == self.event_id,
                EventConfigModel.config_key == key
            ).first()
            
            if config:
                # Update existing config
                config.config_value = str_value
                config.update_number += 1
            else:
                # Create new config
                config = EventConfigModel(
                    event_id=self.event_id,
                    config_key=key,
                    config_value=str_value,
                    long_value=None,
                    update_number=0
                )
                session.add(config)
            
            # Update cache
            if key in self._config_cache:
                self._config_cache[key]["value"] = str_value
                self._config_cache[key]["update_number"] += 1
            else:
                self._config_cache[key] = {
                    "value": str_value,
                    "long_value": None,
                    "update_number": 0
                }
            
            # Commit changes
            session.commit()
            return True
        except Exception as e:
            logger.error(f"Error setting config {key}: {e}")
            session.rollback()
            return False
    
    def _set_long_config(self, key: str, value: Any) -> bool:
        """
        Set a long configuration value (JSON)
        
        Args:
            key: Configuration key
            value: Value to set (will be converted to JSON)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Convert value to JSON
            json_value = json.dumps(value)
            
            # Check if config exists
            config = session.query(EventConfigModel).filter(
                EventConfigModel.event_id == self.event_id,
                EventConfigModel.config_key == key
            ).first()
            
            if config:
                # Update existing config
                config.long_value = json_value
                config.update_number += 1
            else:
                # Create new config
                config = EventConfigModel(
                    event_id=self.event_id,
                    config_key=key,
                    config_value="json",
                    long_value=json_value,
                    update_number=0
                )
                session.add(config)
            
            # Update cache
            if key in self._config_cache:
                self._config_cache[key]["long_value"] = json_value
                self._config_cache[key]["update_number"] += 1
            else:
                self._config_cache[key] = {
                    "value": "json",
                    "long_value": json_value,
                    "update_number": 0
                }
            
            # Commit changes
            session.commit()
            return True
        except Exception as e:
            logger.error(f"Error setting long config {key}: {e}")
            session.rollback()
            return False
    
    # Common properties for all event types
    @property
    def general_notification_channel_id(self) -> int:
        """Channel ID for general notifications"""
        return int(self._get_config("general_notification_channel_id", 0))
    
    @general_notification_channel_id.setter
    def general_notification_channel_id(self, value: int):
        self._set_config("general_notification_channel_id", value)
    
    @property
    def admin_notification_channel_id(self) -> int:
        """Channel ID for admin notifications"""
        return int(self._get_config("admin_notification_channel_id", 0))
    
    @admin_notification_channel_id.setter
    def admin_notification_channel_id(self, value: int):
        self._set_config("admin_notification_channel_id", value)
    
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