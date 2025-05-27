from sqlalchemy import Integer, String, Boolean, ForeignKey, DateTime, func, JSON, Enum
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, List, TYPE_CHECKING, Dict, Any
from datetime import datetime
from enum import Enum as PyEnum
from db.base import Base
from .TaskType import TaskType

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from ..EventModel import EventModel
    from .AssignedTask import AssignedTask




class EventTask(Base):
    """
    Represents a task in an event.
    :var id: The ID of the task
    :var event_id: The ID of the event
    :var name: The name of the task
    :var description: The description of the task
    :var difficulty: The difficulty of the task
    :var task_type: The type of task (item_collection, kc_target, etc.)
    :var points: The points of the task
    :var required_items: The items required to complete the task (legacy for item_collection)
    :var task_config: JSON configuration specific to the task type
    :var date_added: The date and time the task was added
    :var date_updated: The date and time the task was last updated
    """
    __tablename__ = 'event_tasks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.id"))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    difficulty: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    task_type: Mapped[TaskType] = mapped_column(Enum(TaskType), default=TaskType.ITEM_COLLECTION)
    points: Mapped[int] = mapped_column(Integer)
    required_items: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)  # Legacy field
    task_config: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)  # New flexible config
    date_added: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    date_updated: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        event_id: int,
        name: str,
        points: int,
        task_type: TaskType = TaskType.ITEM_COLLECTION,
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        required_items: Optional[Dict[str, Any]] = None,
        task_config: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> None:
        """
        Create a new EventTask instance.
        
        Args:
            event_id: The ID of the event this task belongs to
            name: The name of the task
            points: The point value of the task
            task_type: The type of task (default: ITEM_COLLECTION)
            description: Optional description of the task
            difficulty: Optional difficulty level (e.g., "easy", "medium", "hard")
            required_items: Optional items required (legacy field for item_collection)
            task_config: Optional JSON configuration specific to the task type
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            name=name,
            points=points,
            task_type=task_type,
            description=description,
            difficulty=difficulty,
            required_items=required_items,
            task_config=task_config,
            **kwargs
        )
    
    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="tasks")
    assigned_tasks: Mapped[List["AssignedTask"]] = relationship("AssignedTask", back_populates="task")

    def get_task_requirements(self) -> Optional[Dict[str, Any]]:
        """Get the requirements for this task based on its type."""
        if self.task_type == TaskType.ITEM_COLLECTION:
            return self.required_items
        else:
            return self.task_config
    
    def validate_task_config(self) -> bool:
        """Validate that the task configuration is correct for the task type."""
        if not self.task_config:
            return self.task_type == TaskType.ITEM_COLLECTION
        
        # Add validation logic for each task type
        if self.task_type == TaskType.KC_TARGET:
            required_keys = ["target_kc", "boss_name"]
            return all(key in self.task_config for key in required_keys)
        elif self.task_type == TaskType.XP_TARGET:
            required_keys = ["target_xp", "skill_name"]
            return all(key in self.task_config for key in required_keys)
        elif self.task_type == TaskType.EHP_TARGET:
            required_keys = ["target_ehp"]
            return all(key in self.task_config for key in required_keys)
        elif self.task_type == TaskType.EHB_TARGET:
            required_keys = ["target_ehb"]
            return all(key in self.task_config for key in required_keys)
        elif self.task_type == TaskType.LOOT_VALUE:
            required_keys = ["target_value"]
            return all(key in self.task_config for key in required_keys)
        elif self.task_type == TaskType.TIME_BASED:
            required_keys = ["boss_name", "time_limit_seconds"]
            return all(key in self.task_config for key in required_keys)
        
        return True  # Custom tasks can have any config