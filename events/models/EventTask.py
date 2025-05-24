from sqlalchemy import JSON, Column, Integer, String, Text, Boolean, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship
from db.base import Base


class EventTask(Base):
    __tablename__ = 'event_tasks'

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    difficulty = Column(String(255), nullable=False)
    points = Column(Integer, nullable=False)
    required_items = Column(JSON, nullable=True)
    is_assembly = Column(Boolean, nullable=False)
    assembly_id = Column(Integer, nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    
    # Relationships
    event = relationship("EventModel")
    assigned_tasks = relationship("AssignedTask", back_populates="task")