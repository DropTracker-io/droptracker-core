from sqlalchemy import Integer, String, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, TYPE_CHECKING, List    
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventModel import EventModel
    from .EventTeamModel import EventTeamModel
    from db.models import User, Player
    from .tasks.data import TrackedTaskData


class EventParticipant(Base):
    """
    Represents a participant in an event.
    :var id: The ID of the participant
    :var event_id: The ID of the event
    :var user_id: The ID of the user
    :var player_id: The ID of the player
    :var team_id: The ID of the team
    :var created_at: The date and time the participant was created
    :var updated_at: The date and time the participant was last updated
    :var points: The points of the participant
    """
    __tablename__ = 'event_participants'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.user_id'))
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey('players.player_id'))
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey('event_teams.id'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    points: Mapped[int] = mapped_column(Integer, default=0)

    def __init__(
        self,
        *,
        event_id: int,
        user_id: int,
        player_id: int,
        team_id: Optional[int] = None,
        points: int = 0,
        **kwargs
    ) -> None:
        """
        Create a new EventParticipant instance.
        
        Args:
            event_id: The ID of the event
            user_id: The ID of the user
            player_id: The ID of the player
            team_id: Optional ID of the team
            points: The participant's points (default: 0)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            user_id=user_id,
            player_id=player_id,
            team_id=team_id,
            points=points,
            **kwargs
        )

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="participants")
    user: Mapped["User"] = relationship("User")
    player: Mapped["Player"] = relationship("Player")
    team: Mapped[Optional["EventTeamModel"]] = relationship("EventTeamModel", back_populates="members") 
    tracked_data: Mapped[List["TrackedTaskData"]] = relationship("TrackedTaskData", back_populates="participant")