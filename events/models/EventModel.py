from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP, func
from sqlalchemy.orm import relationship
from db.base import Base

class EventModel(Base): # type: ignore
    """
    Represents an event object in the database. Always has an attached type
    :var id: The ID of the event
    :var name: The name of the event
    :var type: The type of event
    :var description: The description of the event
    :var start_date: The start date of the event
    :var status: The status of the event
    :var author_id: The ID of the author of the event
    """
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False)
    author_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    event_type = Column(String(255), nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    status = Column(String(255), nullable=False)
    banner_image = Column(String(255), nullable=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    start_date = Column(Integer, nullable=True)
    end_date = Column(Integer, nullable=True)
    max_participants = Column(Integer, nullable=True)
    team_size = Column(Integer, nullable=True)
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # The group relationship will be added in setup_relationships()
    
    # Other relationships
    participants = relationship("EventParticipant", back_populates="event")
    configurations = relationship("EventConfigModel", back_populates="event")
    teams = relationship("EventTeamModel", back_populates="event")
    items = relationship("EventShopItem", back_populates="event")
    board_game = relationship("BoardGameModel", back_populates="event")
    assigned_tasks = relationship("AssignedTask", back_populates="event")

