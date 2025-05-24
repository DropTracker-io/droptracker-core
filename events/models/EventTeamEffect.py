from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, text
from sqlalchemy.orm import relationship
from db.base import Base


class EventTeamEffect(Base):
    __tablename__ = 'event_team_effects'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey('event_teams.id', ondelete='CASCADE'), nullable=False)
    effect_name = Column(String(255), nullable=False)
    remaining_turns = Column(Integer, nullable=False)
    expiry_date = Column(DateTime, nullable=False)
    effect_data = Column(Text, nullable=True)  # For any additional effect data as JSON
    created_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    
    # Relationship
    team = relationship("EventTeamModel", back_populates="effects") 