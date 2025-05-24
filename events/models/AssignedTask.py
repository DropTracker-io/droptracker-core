from sqlalchemy import JSON, Column, Integer, String, Text, ForeignKey, TIMESTAMP, func
from sqlalchemy.orm import relationship
from db.base import Base

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

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    team_id = Column(Integer, ForeignKey('event_teams.id'), nullable=False)
    task_id = Column(Integer, ForeignKey('event_tasks.id'), nullable=False)
    status = Column(String(255), nullable=False, default='pending')
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    data = Column(JSON, nullable=False, default={})

    # Relationships
    event = relationship("EventModel", back_populates="assigned_tasks")
    team = relationship("EventTeamModel", back_populates="assigned_tasks")
    task = relationship("EventTask", back_populates="assigned_tasks")

    def get_requirements(self) -> dict:
        """Get the required items for this task."""
        if self.task:
            ## Returns a json object from the parent Task model's required_items column
            return self.task.required_items
        return None
    
    def get_progress(self) -> dict:
        """Get the current progress for this task."""
        current_status = self.data
        required_items = self.get_requirements() 
        for key, value in current_status.items():
            if key in required_items:
                if int(value) >= required_items[key]:
                    required_items.remove(key)
                else:
                    required_items[key] = required_items[key] - int(value)
            else:
                print(f"Tried to remove {key} from {required_items} for this task, but it was not found")
        return required_items

    
    def add_progress(self, item_name, amount):
        """Add progress to this task."""
        if self.data is None:
            self.data = {}
        if item_name in self.data:
            self.data[item_name] = self.data[item_name] + amount
        else:
            self.data[item_name] = amount

    
    def is_completed(self):
        """Check if the task is completed."""
        progress = self.get_progress()
        if len(progress) == 0:
            return True
        return False
    
