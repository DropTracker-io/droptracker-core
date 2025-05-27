from sqlalchemy import JSON, Integer, String, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, Dict, Any, TYPE_CHECKING, List
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from ..EventModel import EventModel
    from ..EventTeamModel import EventTeamModel
    from .EventTask import EventTask
    from .data.TrackedTaskData import TrackedTaskData


class AssignedTask(Base):
    """
    Represents a task assigned to a team in an event.
    :var id: Unique ID for this assigned task (auto-inc)
    :var event_id: The ID of the event (from the event table)
    :var team_id: The ID of the team (from the team table)
    :var task_id: The ID of the task (from the task table)
    :var status: The status of the task (e.g. "pending", "started", "completed", "skipped", "mercy")
    :var created_at: The date and time the task was created
    :var updated_at: The date and time the task was last updated
    :var data: The json-encoded data for the current task progress
    """
    __tablename__ = 'assigned_tasks'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_teams.id'))
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_tasks.id'))
    status: Mapped[str] = mapped_column(String(255), default='pending')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    data: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)

    def __init__(
        self,
        *,
        event_id: int,
        team_id: int,
        task_id: int,
        status: str = 'pending',
        data: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> None:
        """
        Create a new AssignedTask instance.
        
        Args:
            event_id: The ID of the event
            team_id: The ID of the team
            task_id: The ID of the task
            status: The status of the task (default: 'pending')
            data: JSON data for task progress (default: empty dict)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            team_id=team_id,
            task_id=task_id,
            status=status,
            data=data if data is not None else {},
            **kwargs
        )

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="assigned_tasks")
    team: Mapped["EventTeamModel"] = relationship("EventTeamModel", back_populates="assigned_tasks")
    task: Mapped["EventTask"] = relationship("EventTask", back_populates="assigned_tasks")
    tracked_task_data: Mapped[List["TrackedTaskData"]] = relationship("TrackedTaskData", back_populates="assigned_task")

    def get_requirements(self) -> Optional[Dict[str, Any]]:
        """Get the required items for this task."""
        if self.task:
            ## Returns a json object from the parent Task model based on task type
            return self.task.get_task_requirements()
        return None
    
    def get_progress(self) -> Dict[str, Any]:
        """Get the current progress for this task based on task type."""
        if not self.task:
            return {}
            
        current_status = self.data
        requirements = self.get_requirements()
        
        if not requirements:
            return {}
        
        # Handle different task types
        if self.task.task_type.value == "item_collection":
            return self._get_item_collection_progress(current_status, requirements)
        elif self.task.task_type.value == "kc_target":
            return self._get_kc_progress(current_status, requirements)
        elif self.task.task_type.value == "xp_target":
            return self._get_xp_progress(current_status, requirements)
        elif self.task.task_type.value == "ehp_target":
            return self._get_ehp_progress(current_status, requirements)
        elif self.task.task_type.value == "ehb_target":
            return self._get_ehb_progress(current_status, requirements)
        elif self.task.task_type.value == "loot_value":
            return self._get_loot_value_progress(current_status, requirements)
        else:
            # Custom or unknown task types - use legacy item collection logic
            return self._get_item_collection_progress(current_status, requirements)
    
    def _get_item_collection_progress(self, current_status: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate progress for item collection tasks."""
        remaining = requirements.copy()
        
        for item_name, current_amount in current_status.items():
            if item_name in remaining:
                required_amount = remaining[item_name]
                if int(current_amount) >= required_amount:
                    remaining.pop(item_name, None)
                else:
                    remaining[item_name] = required_amount - int(current_amount)
        if len(remaining) == 0:
            self.status = "completed"
            self.updated_at = datetime.now()
        return remaining
    
    def _get_kc_progress(self, current_status: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate progress for KC target tasks."""
        boss_name = requirements.get("boss_name")
        target_kc = requirements.get("target_kc", 0)
        current_kc = current_status.get(f"{boss_name}_kc", 0)
        
        remaining_kc = max(0, target_kc - current_kc)
        
        return {
            "boss_name": boss_name,
            "target_kc": target_kc,
            "current_kc": current_kc,
            "remaining_kc": remaining_kc,
            "completed": remaining_kc == 0
        }
    
    def _get_xp_progress(self, current_status: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate progress for XP target tasks."""
        skill_name = requirements.get("skill_name")
        target_xp = requirements.get("target_xp", 0)
        current_xp = current_status.get(f"{skill_name}_xp", 0)
        
        remaining_xp = max(0, target_xp - current_xp)
        
        return {
            "skill_name": skill_name,
            "target_xp": target_xp,
            "current_xp": current_xp,
            "remaining_xp": remaining_xp,
            "completed": remaining_xp == 0
        }
    
    def _get_ehp_progress(self, current_status: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate progress for EHP target tasks."""
        target_ehp = requirements.get("target_ehp", 0)
        current_ehp = current_status.get("ehp", 0)
        
        remaining_ehp = max(0, target_ehp - current_ehp)
        
        return {
            "target_ehp": target_ehp,
            "current_ehp": current_ehp,
            "remaining_ehp": remaining_ehp,
            "completed": remaining_ehp == 0
        }
    
    def _get_ehb_progress(self, current_status: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate progress for EHB target tasks."""
        target_ehb = requirements.get("target_ehb", 0)
        current_ehb = current_status.get("ehb", 0)
        
        remaining_ehb = max(0, target_ehb - current_ehb)
        
        return {
            "target_ehb": target_ehb,
            "current_ehb": current_ehb,
            "remaining_ehb": remaining_ehb,
            "completed": remaining_ehb == 0
        }
    
    def _get_loot_value_progress(self, current_status: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate progress for loot value tasks."""
        target_value = requirements.get("target_value", 0)
        source_npc = requirements.get("source_npc")  # Optional - if None, any source
        
        if source_npc:
            current_value = current_status.get(f"{source_npc}_loot_value", 0)
        else:
            current_value = current_status.get("total_loot_value", 0)
        
        remaining_value = max(0, target_value - current_value)
        
        return {
            "target_value": target_value,
            "current_value": current_value,
            "remaining_value": remaining_value,
            "source_npc": source_npc,
            "completed": remaining_value == 0
        }
    
    def _get_time_based_progress(self, current_status: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        """Calculate progress for time-based tasks (e.g., kill boss under X seconds)."""
        boss_name = requirements.get("boss_name")
        time_limit_seconds = requirements.get("time_limit_seconds", 0)
        best_time = current_status.get(f"{boss_name}_best_time")
        completed = best_time is not None and best_time <= time_limit_seconds
        
        return {
            "boss_name": boss_name,
            "time_limit_seconds": time_limit_seconds,
            "best_time_seconds": best_time,
            "completed": completed
        }

    def add_progress(self, progress_type: str, amount: Any) -> None:
        """Add progress to this task based on the progress type."""
        if self.data is None:
            self.data = {}
        
        # Handle different types of progress updates
        if progress_type in self.data:
            # For numeric values, add them together
            if isinstance(amount, (int, float)) and isinstance(self.data[progress_type], (int, float)):
                self.data[progress_type] = self.data[progress_type] + amount
            else:
                # For non-numeric or mixed types, replace the value
                self.data[progress_type] = amount
        else:
            self.data[progress_type] = amount

    def is_completed(self) -> bool:
        """Check if the task is completed based on task type."""
        if not self.task:
            return False
            
        progress = self.get_progress()
        
        if self.task.task_type.value == "item_collection":
            # For item collection, completed when no items remain
            return len(progress) == 0
        else:
            # For other task types, check the 'completed' field in progress
            return progress.get("completed", False)
    
