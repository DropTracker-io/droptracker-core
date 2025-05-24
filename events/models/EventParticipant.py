from sqlalchemy import Column, Integer, String, ForeignKey, TIMESTAMP, func
from sqlalchemy.orm import relationship
from db.base import Base


class EventParticipant(Base):
    __tablename__ = 'event_participants'

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.user_id'), nullable=False)
    player_id = Column(Integer, ForeignKey('players.player_id'), nullable=False)
    team_id = Column(Integer, ForeignKey('event_teams.id'), nullable=True)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    points = Column(String(255), nullable=False, default=0)

    # Relationships
    event = relationship("EventModel", back_populates="participants")
    # Use string references for User and Player
    user = relationship("User")
    player = relationship("Player")
    team = relationship("EventTeamModel", back_populates="members") 