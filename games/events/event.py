import asyncio
import json
import logging
import traceback
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any, Tuple, Union, Type

import interactions
from sqlalchemy import text

from db.eventmodels import (
    EventModel as EventModel, 
    EventConfigModel, 
    EventTeamModel, 
    EventTeamInventory, 
    EventParticipant, 
    session
)
from db.models import Group, User
from games.events.utils.shared import get_event_by_id
from games.events.utils.classes.base import EventType, Task
from games.events.utils.event_config import EventConfig
from games.events.utils.bg_config import BoardGameConfig
from games.events.utils.config_factory import get_config_for_event
logger = logging.getLogger(__name__)


class Event:
    def __init__(self, group_id: int = -1, id: int = -1, notification_channel_id: Optional[int] = None, bot=None):
        """
        Initialize the event
        
        Args:
            group_id: The ID of the group
            id: The ID of the event, if it already existed
            notification_channel_id: ID of the channel to send notifications to
            bot: Discord bot instance
        """
        self.id = id
        self.event_id = id  # Alias for compatibility
        self.group_id = group_id
        self.notification_channel_id = notification_channel_id
        self.bot: interactions.Client = bot
        self.event_type = EventType.BOARD_GAME
        
        # Initialize empty collections
        self.participants = []
        self.teams = []
        
        # Load or create the event
        if self.id == -1:
            asyncio.run(self.create())
        else:
            # Load event data from database
            self.load_event_data()
            
            # Load configuration using factory
            self.config = get_config_for_event(self.id)
            if not self.config:
                logger.warning(f"Failed to load configuration for event {self.id}, creating default")
                self.config = EventConfig(self.id)
    
    async def create(self, author_id: Optional[int] = None) -> None:
        """
        Create a new event in the database
        
        Args:
            author_id: ID of the user creating the event
        """
        self.author_id = author_id
        event = EventModel(
            name=f"Group {self.group_id}'s Event",
            type=self.event_type.value,
            description="An Old School RuneScape event",
            start_date=datetime.now(),
            status="startup",
            author_id=self.author_id,
            group_id=self.group_id
        )
        session.add(event)
        session.commit()
        self.id = event.id
        self.event_id = event.id  # Alias for compatibility

        
        # Create default configuration
        self.config = EventConfig(self.id)
        self.config
        
        logger.info(f"A new {self.event_type.value} event has been created in the database with id {self.id}")
    
    def load_event_data(self):
        """Load event data from database"""
        try:
            # Load event model
            self.event_model = session.query(EventModel).filter(EventModel.id == self.id).first()
            if not self.event_model:
                logger.error(f"Event {self.id} not found in database")
                return
            
            # Set properties from event model
            self.group_id = self.event_model.group_id
            
            # Load event config
            self.load_config()
            
            # Load participants
            self.participants = session.query(EventParticipant).filter(
                EventParticipant.event_id == self.id
            ).all()
            
            # Load teams
            self.teams = session.query(EventTeamModel).filter(
                EventTeamModel.event_id == self.id
            ).all()
            
        except Exception as e:
            logger.error(f"Error loading event data: {e}")
            logger.error(traceback.format_exc())
    
    def load_config(self):
        """Load event configuration from database"""
        try:
            # Load config from database
            self.config = get_config_for_event(self.id)
            if not self.config:
                logger.error(f"Error loading event config: EventConfig unable to be loaded")
                return False
            return True
        except Exception as e:
            logger.error(f"Error loading event config: {e}")
            logger.error(traceback.format_exc())
    
    def _get_point_awards(self, task: Task = None) -> List[Task]:
        """
        Get the point awards for a given task type
        
        Args:
            task: The task to get point awards for
            
        """
        print("POINT AWARDS ARE CURRENTLY 1 EVERYWHERE -- THIS SHOULD HAVE BEEN OVERRIDDEN IN THE EVENT SUBCLASS")
        return 1
    
    def get_participants(self) -> List[EventParticipant]:
        """
        Get all participants in the event
        
        Returns:
            List of EventParticipant objects
        """
        return self.participants
    
    def get_teams(self) -> List[EventTeamModel]:
        """
        Get all teams in the event
        
        Returns:
            List of EventTeam objects
        """
        return self.teams
    
    
    def add_participant(self, user_id: int, team_id: Optional[int] = None) -> bool:
        """
        Add a participant to the event
        
        Args:
            user_id: User ID
            team_id: Team ID (optional)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Check if participant already exists
            participant = session.query(EventParticipant).filter(
                EventParticipant.event_id == self.id,
                EventParticipant.user_id == user_id
            ).first()
            
            if participant:
                # Update team if provided
                if team_id is not None:
                    participant.team_id = team_id
            else:
                # Create new participant
                participant = EventParticipant(
                    event_id=self.id,
                    user_id=user_id,
                    team_id=team_id
                )
                session.add(participant)
            
            # Commit changes
            session.commit()
            
            # Reload participants
            self.participants = session.query(EventParticipant).filter(
                EventParticipant.event_id == self.id
            ).all()
            teams_sorted = sorted(self.teams, key=lambda x: x.id)
            channel_id = None
            for i, team in enumerate(teams_sorted):
                if team.id == team_id:
                    match i:
                        case 0:
                            channel_id = self.config.team_channel_id_1
                            name = self.config.team_name_1
                            role_id = self.config.team_role_id_1
                        case 1:
                            channel_id = self.config.team_channel_id_2
                            role_id = self.config.team_role_id_2
                        case 2:
                            channel_id = self.config.team_channel_id_3
                            role_id = self.config.team_role_id_3
                        case 3:
                            channel_id = self.config.team_channel_id_4
                            role_id = self.config.team_role_id_4
                        case _:
                            return False
            if channel_id:
                channel = self.bot.fetch_channel(channel_id=channel_id)
                if channel:
                    pass
                    #embed = await self.
            return True
        except Exception as e:
            logger.error(f"Error adding participant: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def remove_participant(self, user_id: int) -> bool:
        """
        Remove a participant from the event
        
        Args:
            user_id: User ID
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Find participant
            participant = session.query(EventParticipant).filter(
                EventParticipant.event_id == self.id,
                EventParticipant.user_id == user_id
            ).first()
            
            if not participant:
                logger.warning(f"Participant {user_id} not found in event {self.id}")
                return False
            
            # Remove participant
            session.delete(participant)
            session.commit()
            
            # Reload participants
            self.participants = session.query(EventParticipant).filter(
                EventParticipant.event_id == self.id
            ).all()
            
            return True
            
        except Exception as e:
            logger.error(f"Error removing participant: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def create_team(self, name: str, role_id: Optional[int] = None) -> Optional[int]:
        """
        Create a new team
        
        Args:
            name: Team name
            role_id: Discord role ID (optional)
            
        Returns:
            Team ID if successful, None otherwise
        """
        try:
            # Create new team
            team = EventTeamModel(
                event_id=self.id,
                name=name,
                role_id=role_id
            )
            session.add(team)
            session.commit()
            
            # Reload teams
            self.teams = session.query(EventTeamModel).filter(
                EventTeamModel.event_id == self.id
            ).all()
            
            return team.id
            
        except Exception as e:
            logger.error(f"Error creating team: {e}")
            logger.error(traceback.format_exc())
            return None
    
    def delete_team(self, team_id: int) -> bool:
        """
        Delete a team
        
        Args:
            team_id: Team ID
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Find team
            team = session.query(EventTeamModel).filter(
                EventTeamModel.id == team_id,
                EventTeamModel.event_id == self.id
            ).first()
            
            if not team:
                logger.warning(f"Team {team_id} not found in event {self.id}")
                return False
            
            # Remove all participants from team
            participants = session.query(EventParticipant).filter(
                EventParticipant.team_id == team_id
            ).all()
            
            for participant in participants:
                participant.team_id = None
            
            # Delete team
            session.delete(team)
            session.commit()
            
            # Reload teams
            self.teams = session.query(EventTeamModel).filter(
                EventTeamModel.event_id == self.id
            ).all()
            
            return True
            
        except Exception as e:
            logger.error(f"Error deleting team: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def game_loop(self) -> None:
        """
        Main game loop - should be implemented by subclasses
        """
        raise NotImplementedError("Subclasses must implement game_loop()")
    
    def check_task(self, player_id: str, item_name: str) -> bool:
        """
        Check if a player has completed a task - should be implemented by subclasses
        """
        raise NotImplementedError("Subclasses must implement check_task()")
    
    def save_game_state(self) -> bool:
        """
        Save the current game state - should be implemented by subclasses
        """
        raise NotImplementedError("Subclasses must implement save_game_state()")
    
    def get_event_by_uid(self, discord_id):
        # Get user
        user = session.query(User).filter(User.discord_id == str(discord_id)).first()
        if not user:
            return None
        
        # Get user's groups
        stmt = text("SELECT group_id FROM user_group_association WHERE user_id = :user_id")
        groups = session.execute(stmt, {"user_id": user.user_id}).fetchall()
        print(f"Found {len(groups)} groups for user: {user.user_id}")
        for group in groups:
            group_id = group[0]
            group: Group = session.query(Group).filter(Group.group_id == group_id).first()
            event = session.query(EventModel).filter(EventModel.group_id == group.group_id).first()
            game = get_event_by_id(event.id)
            if game and game.status == "active":
                return game
        print("No event found that is active for this user in group: ", group.group_id)
        return None
    
    async def get_user_team(self, user_id: int) -> Tuple[Optional[EventTeamModel], Optional[str]]:
        """
        Get the team a user belongs to in an event
        
        Args:
            user_id: The Discord user ID
            
        Returns:
            Tuple of (EventTeam object, team_name) or (None, None) if not found
        """
        try:
            # Find the user in the database
            user = session.query(User).filter(User.discord_id == str(user_id)).first()
            if not user:
                return None, None
                
            # Find the participant entry
            participant = session.query(EventParticipant).filter(
                EventParticipant.event_id == self.id,
                EventParticipant.user_id == user.user_id
            ).first()
            #print("Found event participant: ", participant)
            if not participant:
                return None, None
                
            # Get the team
            team: EventTeamModel = session.query(EventTeamModel).get(participant.team_id)
            if not team:
                return None, None
                
            return team, team.name
        except Exception as e:
            logger.error(f"Database error in get_user_team: {e}")
            return None, None