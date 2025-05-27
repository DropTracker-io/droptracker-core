from sqlalchemy import JSON, Integer, String, ForeignKey, DateTime, func, Enum
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, Dict, Any, TYPE_CHECKING
from datetime import datetime
from db.base import Base
from events.models.tasks.TaskType import TaskType

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from ...EventModel import EventModel
    from ...EventTeamModel import EventTeamModel
    from ..AssignedTask import AssignedTask
    from ...EventParticipant import EventParticipant


class TrackedTaskData(Base):
    """
    Represents tracked data for a task assigned to a participant in an event.
    :var id: Unique ID for this tracked task data (auto-inc)
    :var event_id: The ID of the event (from the event table)
    :var team_id: The ID of the team (from the team table)
    :var assigned_task_id: The ID of the AssignedTask
    :var player_id: The ID of the participant (from the event_participants table)
    :var key: The key of the tracked data
    :var value: The value of the tracked data
    """
    __tablename__ = 'tracked_task_data'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_teams.id'))
    assigned_task_id: Mapped[int] = mapped_column(Integer, ForeignKey('assigned_tasks.id'))
    player_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_participants.id'))
    status: Mapped[str] = mapped_column(String(255))
    type: Mapped[TaskType] = mapped_column(Enum(TaskType))
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    def __init__(
        self,   
        *,
        event_id: int,
        team_id: int,
        assigned_task_id: int,
        player_id: int,
        status: str,
        type: TaskType,
        key: str,
        value: str,
        **kwargs
    ) -> None:
        """
        Create a new TrackedTaskData instance.
        
        Args:
            event_id: The ID of the event
            team_id: The ID of the team
            assigned_task_id: The ID of the assigned task
            player_id: The ID of the participant
            status: The status of the data (creation defines first entry for task on initialization)
            type: The type of the task
            key: The key of the data
            value: The value of the data
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            team_id=team_id,
            assigned_task_id=assigned_task_id,
            player_id=player_id,
            status=status,
            type=type,
            key=key,
            value=value,
            **kwargs
        )

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="tracked_task_data")
    team: Mapped["EventTeamModel"] = relationship("EventTeamModel", back_populates="tracked_task_data")
    assigned_task: Mapped["AssignedTask"] = relationship("AssignedTask", back_populates="tracked_task_data")
    participant: Mapped["EventParticipant"] = relationship("EventParticipant", back_populates="tracked_data")