from sqlalchemy import Integer, String, Text, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventParticipant import EventParticipant
    from .EventConfigModel import EventConfigModel
    from .EventTeamModel import EventTeamModel
    from .EventShopItem import EventShopItem
    from .tasks.EventTask import EventTask
    from .tasks.AssignedTask import AssignedTask
    from .tasks.data.TrackedTaskData import TrackedTaskData
    from .types.BoardGame import BoardGameModel
    from .types.bingo import BingoBoardModel, BingoGameModel
    from db.models import Group, User


class EventModel(Base):
    """
    Represents an event object in the database. Always has an attached type
    :var id: The ID of the event
    :var group_id: The ID of the group
    :var author_id: The ID of the author of the event
    :var event_type: The type of event
    :var created_at: When the event was created
    :var status: The status of the event
    :var banner_image: URL to the banner image
    :var title: The title of the event
    :var description: The description of the event
    :var start_date: The start date of the event (timestamp)
    :var end_date: The end date of the event (timestamp)
    :var max_participants: Maximum number of participants
    :var team_size: Size of teams
    :var updated_at: When the event was last updated
    """
    __tablename__ = 'events'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey('groups.group_id'))
    author_id: Mapped[int] = mapped_column(Integer, ForeignKey('users.user_id'))
    event_type: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    status: Mapped[str] = mapped_column(String(255))
    banner_image: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_date: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    end_date: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_participants: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    team_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        group_id: int,
        author_id: int,
        event_type: str,
        status: str,
        title: str,
        banner_image: Optional[str] = None,
        description: Optional[str] = None,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None,
        max_participants: Optional[int] = None,
        team_size: Optional[int] = None,
        **kwargs
    ) -> None:
        """
        Create a new EventModel instance.
        
        Args:
            group_id: The ID of the group hosting the event
            author_id: The ID of the user who created the event
            event_type: The type of event (e.g., "board_game", "race", etc.)
            status: The current status of the event (e.g., "pending", "active", "completed")
            title: The title/name of the event
            banner_image: Optional URL to a banner image
            description: Optional description of the event
            start_date: Optional start date as timestamp
            end_date: Optional end date as timestamp
            max_participants: Optional maximum number of participants
            team_size: Optional size of teams for team-based events
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            group_id=group_id,
            author_id=author_id,
            event_type=event_type,
            status=status,
            title=title,
            banner_image=banner_image,
            description=description,
            start_date=start_date,
            end_date=end_date,
            max_participants=max_participants,
            team_size=team_size,
            **kwargs
        )
    
    # Relationships with proper type hints
    # Note: group relationship will be added in setup_relationships()
    participants: Mapped[List["EventParticipant"]] = relationship("EventParticipant", back_populates="event")
    configurations: Mapped[List["EventConfigModel"]] = relationship("EventConfigModel", back_populates="event")
    teams: Mapped[List["EventTeamModel"]] = relationship("EventTeamModel", back_populates="event")
    items: Mapped[List["EventShopItem"]] = relationship("EventShopItem", back_populates="event")
    board_game: Mapped[Optional["BoardGameModel"]] = relationship("BoardGameModel", back_populates="event", uselist=False)
    bingo_game: Mapped[Optional["BingoGameModel"]] = relationship("BingoGameModel", back_populates="event", uselist=False)
    bingo_boards: Mapped[List["BingoBoardModel"]] = relationship("BingoBoardModel", back_populates="event")
    tasks: Mapped[List["EventTask"]] = relationship("EventTask", back_populates="event")
    assigned_tasks: Mapped[List["AssignedTask"]] = relationship("AssignedTask", back_populates="event")
    tracked_task_data: Mapped[List["TrackedTaskData"]] = relationship("TrackedTaskData", back_populates="event")

