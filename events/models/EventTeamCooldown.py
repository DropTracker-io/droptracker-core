from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, text
from sqlalchemy.orm import relationship
from db.base import Base


class EventTeamCooldown(Base):
    __tablename__ = 'event_team_cooldowns'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    team_id = Column(Integer, ForeignKey('event_teams.id', ondelete='CASCADE'), nullable=False)
    cooldown_name = Column(String(255), nullable=False)
    remaining_turns = Column(Integer, nullable=False)
    expiry_date = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    updated_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))
    
    # Relationship
    team = relationship("EventTeamModel", back_populates="cooldowns") 