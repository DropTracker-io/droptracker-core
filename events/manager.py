from events.models.tasks import EventTask, AssignedTask
from events.models import EventModel, BingoGameModel, EventConfigModel, EventTeamModel
from db.models import session
from typing import List
import random

class EventManager:
    def __init__(self):
        pass

    def generate_task(self, event: EventModel, team: EventTeamModel):
        if event.event_type == "bingo":
            event_config = event.configurations
            event_config: List[EventConfigModel] = event_config
            difficulty = 0 ## Start the task at 0 and increase based on the configured offset by the group
            for config in event_config:
                if config.config_key == "base_difficulty":
                    difficulty += int(config.config_value)
                    difficulty += random.randint(0, 4 - difficulty)
                if config.config_key == "static_difficulty":
                    difficulty = int(config.config_value)
            difficulty = 4 if difficulty > 4 else difficulty ## Cap the difficulty at 4
            available_tasks = session.query(EventTask).filter(
                EventTask.event_id == event.id,
                EventTask.difficulty == str(difficulty)  # Convert to string since difficulty is String(50)
            ).all()
            
            if len(available_tasks) == 0:
                print(f"No tasks were found for event {event.id}, difficulty {difficulty}, forced to return None")
                return None
            
            task = random.choice(available_tasks)
            
            assigned_task = AssignedTask(
                event_id=event.id,
                task_id=task.id,    
                team_id=team.id,
                status="pending",
                data={}  # Use data field instead of progress
            )
            session.add(assigned_task)
            session.commit()
            return assigned_task
        else:
            return None