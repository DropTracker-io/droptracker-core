from sqlalchemy import Integer, String, Boolean, ForeignKey, DateTime, func, JSON, Enum
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, List, TYPE_CHECKING, Dict, Any
from datetime import datetime
from db.base import Base
from .TaskType import TaskType

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventTask import EventTask


class BaseTask(Base):
    """
    Represents a base task template that can be duplicated for use in any event.
    These serve as a library of pre-configured tasks that event organizers can choose from.
    
    :var id: The ID of the base task
    :var name: The name of the task
    :var description: The description of the task
    :var difficulty: The difficulty of the task
    :var task_type: The type of task (item_collection, kc_target, etc.)
    :var points: The default points for the task (can be overridden when creating EventTask)
    :var required_items: The items required to complete the task (legacy for item_collection)
    :var task_config: JSON configuration specific to the task type
    :var is_active: Whether this base task is available for use
    :var created_by: Optional user ID who created this base task
    :var date_added: The date and time the task was added
    :var date_updated: The date and time the task was last updated
    """
    __tablename__ = 'base_tasks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    difficulty: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    task_type: Mapped[TaskType] = mapped_column(Enum(TaskType), default=TaskType.ITEM_COLLECTION)
    points: Mapped[int] = mapped_column(Integer)
    required_items: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)  # Legacy field
    task_config: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)  # New flexible config
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # User ID who created this
    date_added: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    date_updated: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        name: str,
        points: int,
        task_type: TaskType = TaskType.ITEM_COLLECTION,
        description: Optional[str] = None,
        difficulty: Optional[str] = None,
        required_items: Optional[Dict[str, Any]] = None,
        task_config: Optional[Dict[str, Any]] = None,
        is_active: bool = True,
        created_by: Optional[int] = None,
        **kwargs
    ) -> None:
        """
        Create a new BaseTask instance.
        
        Args:
            name: The name of the task
            points: The default point value of the task
            task_type: The type of task (default: ITEM_COLLECTION)
            description: Optional description of the task
            difficulty: Optional difficulty level (e.g., "easy", "medium", "hard")
            required_items: Optional items required (legacy field for item_collection)
            task_config: Optional JSON configuration specific to the task type
            is_active: Whether this base task is available for use
            created_by: Optional user ID who created this base task
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            name=name,
            points=points,
            task_type=task_type,
            description=description,
            difficulty=difficulty,
            required_items=required_items,
            task_config=task_config,
            is_active=is_active,
            created_by=created_by,
            **kwargs
        )

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
        
        # Add validation logic for each task type (same as EventTask)
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
        
        return True  # Custom tasks can have any config

    def to_event_task(self, event_id: int, points_override: Optional[int] = None) -> "EventTask":
        """
        Create an EventTask instance from this BaseTask template.
        
        Args:
            event_id: The ID of the event to create the task for
            points_override: Optional override for the points value
            
        Returns:
            A new EventTask instance based on this BaseTask
        """
        from .EventTask import EventTask
        
        return EventTask(
            event_id=event_id,
            name=self.name,
            points=points_override if points_override is not None else self.points,
            task_type=self.task_type,
            description=self.description,
            difficulty=self.difficulty,
            required_items=self.required_items,
            task_config=self.task_config
        )

    def get_preview_text(self) -> str:
        """Get a human-readable preview of what this task requires."""
        if self.task_type == TaskType.ITEM_COLLECTION and self.required_items:
            items_text = ", ".join([f"{qty}x {item}" for item, qty in self.required_items.items()])
            return f"Collect: {items_text}"
        
        elif self.task_type == TaskType.KC_TARGET and self.task_config:
            boss_name = self.task_config.get("boss_name", "Unknown")
            target_kc = self.task_config.get("target_kc", 0)
            return f"Defeat {boss_name} {target_kc} times"
        
        elif self.task_type == TaskType.XP_TARGET and self.task_config:
            skill_name = self.task_config.get("skill_name", "Unknown")
            target_xp = self.task_config.get("target_xp", 0)
            return f"Gain {target_xp:,} {skill_name} XP"
        
        elif self.task_type == TaskType.EHP_TARGET and self.task_config:
            target_ehp = self.task_config.get("target_ehp", 0)
            return f"Achieve {target_ehp} EHP"
        
        elif self.task_type == TaskType.EHB_TARGET and self.task_config:
            target_ehb = self.task_config.get("target_ehb", 0)
            return f"Achieve {target_ehb} EHB"
        
        elif self.task_type == TaskType.LOOT_VALUE and self.task_config:
            target_value = self.task_config.get("target_value", 0)
            source_npc = self.task_config.get("source_npc")
            value_text = f"{target_value:,} GP worth of loot"
            if source_npc:
                return f"Collect {value_text} from {source_npc}"
            else:
                return f"Collect {value_text} from any source"
        
        else:
            return self.description or "Custom task"

    @classmethod
    def get_active_tasks(cls, task_type: Optional[TaskType] = None, difficulty: Optional[str] = None):
        """
        Get active base tasks, optionally filtered by type and difficulty.
        
        Args:
            task_type: Optional filter by task type
            difficulty: Optional filter by difficulty
            
        Returns:
            Query for active base tasks
        """
        from sqlalchemy.orm import Session
        # Note: You'll need to pass the session from your calling code
        # This is just the query structure
        query_filters = [cls.is_active == True]
        
        if task_type:
            query_filters.append(cls.task_type == task_type)
        
        if difficulty:
            query_filters.append(cls.difficulty == difficulty)
        
        # Return the filter conditions for use with session.query()
        return query_filters

    def __repr__(self) -> str:
        return f"<BaseTask(id={self.id}, name='{self.name}', type='{self.task_type.value}', points={self.points})>"