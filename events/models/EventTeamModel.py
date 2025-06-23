from sqlalchemy import Integer, String, ForeignKey, DateTime, func, text
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventModel import EventModel
    from .EventParticipant import EventParticipant
    from .EventTeamInventory import EventTeamInventory
    from .EventTeamCooldown import EventTeamCooldown
    from .EventTeamEffect import EventTeamEffect
    from .tasks.AssignedTask import AssignedTask
    from .tasks.data.TrackedTaskData import TrackedTaskData
    from .types.bingo import BingoBoardModel


class EventTeamModel(Base):
    """
    Represents a team in an event in the database.
    :var id: The ID of the team
    :var event_id: The ID of the event
    :var name: The name of the team
    :var current_location: The current location of the team
    :var previous_location: The previous location of the team
    :var points: The number of points the team has
    :var gold: The number of gold the team has
    :var created_at: The date and time the team was created
    :var updated_at: The date and time the team was last updated
    :var current_task: The current task the team is working on
    :var task_progress: The progress of the current task
    :var turn_number: The number of turns the team has taken
    :var mercy_rule: The date and time the mercy rule was last updated
    :var mercy_count: The number of times the mercy rule has been used
    """
    __tablename__ = 'event_teams'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    name: Mapped[str] = mapped_column(String(255))
    current_location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    previous_location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    gold: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    current_task: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    task_progress: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    turn_number: Mapped[int] = mapped_column(Integer, default=1)
    mercy_rule: Mapped[Optional[datetime]] = mapped_column(
        DateTime, 
        nullable=True
    )
    mercy_count: Mapped[int] = mapped_column(Integer, default=0)

    def __init__(
        self,
        *,
        event_id: int,
        name: str,
        current_location: Optional[str] = None,
        previous_location: Optional[str] = None,
        points: int = 0,
        gold: int = 100,
        current_task: Optional[int] = None,
        task_progress: Optional[int] = None,
        turn_number: int = 1,
        mercy_count: int = 0,
        **kwargs
    ) -> None:
        """
        Create a new EventTeamModel instance.
        
        Args:
            event_id: The ID of the event this team belongs to
            name: The name of the team
            current_location: Optional current location of the team
            previous_location: Optional previous location of the team
            points: The team's points (default: 0)
            gold: The team's gold (default: 100)
            current_task: Optional ID of the current task
            task_progress: Optional progress on the current task
            turn_number: The current turn number (default: 1)
            mercy_count: Number of mercy rule applications (default: 0)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            name=name,
            current_location=current_location,
            previous_location=previous_location,
            points=points,
            gold=gold,
            current_task=current_task,
            task_progress=task_progress,
            turn_number=turn_number,
            mercy_count=mercy_count,
            **kwargs
        )

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="teams")
    members: Mapped[List["EventParticipant"]] = relationship("EventParticipant", back_populates="team")
    inventory: Mapped[List["EventTeamInventory"]] = relationship("EventTeamInventory", back_populates="team")
    cooldowns: Mapped[List["EventTeamCooldown"]] = relationship("EventTeamCooldown", back_populates="team")
    effects: Mapped[List["EventTeamEffect"]] = relationship("EventTeamEffect", back_populates="team")
    assigned_tasks: Mapped[List["AssignedTask"]] = relationship("AssignedTask", back_populates="team")
    tracked_task_data: Mapped[List["TrackedTaskData"]] = relationship("TrackedTaskData", back_populates="team")
    bingo_boards: Mapped[List["BingoBoardModel"]] = relationship("BingoBoardModel", back_populates="team") 