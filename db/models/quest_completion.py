from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base


class QuestCompletionEntry(Base):
    __tablename__ = "quest_completions"
    __table_args__ = {
        "extend_existing": True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True, nullable=False)
    quest_name = Column(String(255), nullable=False)
    quests_completed = Column(Integer, nullable=True)
    total_quests = Column(Integer, nullable=True)
    completion_percentage = Column(String(20), nullable=True)
    quest_points = Column(Integer, nullable=True)
    total_quest_points = Column(Integer, nullable=True)
    qp_percentage = Column(String(20), nullable=True)
    timestamp = Column(Integer, nullable=True)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True, unique=True, index=True)

    player = relationship("Player", back_populates="quests")

