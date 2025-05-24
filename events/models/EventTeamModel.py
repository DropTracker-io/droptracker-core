from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP, func, text
from sqlalchemy.orm import relationship
from db.base import Base


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

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    name = Column(String(255), nullable=False)
    current_location = Column(String(255), nullable=True)
    previous_location = Column(String(255), nullable=True)
    points = Column(Integer, nullable=False, default=0)
    gold = Column(Integer, nullable=False, default=100)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    current_task = Column(Integer, nullable=True)
    task_progress = Column(Integer, nullable=True)
    turn_number = Column(Integer, nullable=False, default=1)
    mercy_rule = Column(
        TIMESTAMP, 
        server_default=text("CURRENT_TIMESTAMP + INTERVAL 1 DAY"),
        nullable=True
    )
    mercy_count = Column(Integer, nullable=False, default=0)

    # Relationships
    event = relationship("EventModel", back_populates="teams")
    members = relationship("EventParticipant", back_populates="team")
    inventory = relationship("EventTeamInventory", back_populates="team")
    cooldowns = relationship("EventTeamCooldown", back_populates="team")
    effects = relationship("EventTeamEffect", back_populates="team")
    assigned_tasks = relationship("AssignedTask", back_populates="team") 