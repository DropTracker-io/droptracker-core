from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP, func
from sqlalchemy.orm import relationship
from db.base import Base


class EventConfigModel(Base):
    """
    Represents a configuration for an event in the database.
    :var id: The ID of the configuration
    :var event_id: The ID of the event
    :var config_key: The key of the configuration
    :var config_value: The value of the configuration
    :var long_value: The long value of the configuration
    :var update_number: The number of updates to the configuration
    :var created_at: The date and time the configuration was created
    :var updated_at: The date and time the configuration was last updated
    """
    __tablename__ = 'event_configurations'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    config_key = Column(String(255), nullable=False)
    config_value = Column(String(255), nullable=False)
    long_value = Column(Text, nullable=True)
    update_number = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # Relationship
    event = relationship("EventModel", back_populates="configurations") 