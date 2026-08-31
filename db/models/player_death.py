from sqlalchemy import BigInteger, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base


class PlayerDeath(Base):
    __tablename__ = "player_deaths"
    __table_args__ = {
        "extend_existing": True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True, nullable=False)
    source = Column(String(255), nullable=True)  # killer NPC/player name, if known
    region_id = Column(Integer, nullable=True)
    location = Column(String(255), nullable=True)
    # Area name the server resolved the region to ("Chambers of Xeric").
    region_name = Column(String(125), nullable=True)
    # "npc" | "player" | "unknown" — what the plugin attributed the kill to.
    killer_type = Column(String(16), nullable=True)
    is_pvp = Column(Boolean, nullable=True)
    # Whether the location cost the player anything. NULL means the submission
    # predates the flag (plugin < 6.0) and could not be classified from the
    # region either — distinct from False, which is a real "this one hurt".
    is_safe_death = Column(Boolean, nullable=True)
    # GE value of the items that dropped (plugin 6.0.4+). NULL is "unknown",
    # never "nothing" — BigInteger because a max-cash-stack death overflows int.
    value_lost = Column(BigInteger, nullable=True)
    world_type = Column(String(20), nullable=False, default="main")
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True, unique=True, index=True)

    player = relationship("Player", back_populates="deaths")
