from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base


class DiaryCompletionEntry(Base):
    __tablename__ = "diary_completions"
    __table_args__ = {
        "extend_existing": True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True, nullable=False)
    diary_name = Column(String(255), nullable=False)  # area, e.g. "Ardougne"
    diary_tier = Column(String(20), nullable=True)  # Easy / Medium / Hard / Elite
    world_type = Column(String(20), nullable=False, default="main")
    timestamp = Column(Integer, nullable=True)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True, unique=True, index=True)

    player = relationship("Player", back_populates="diaries")
