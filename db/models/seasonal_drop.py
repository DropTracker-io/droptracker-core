from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy import func

from .base import Base
from .drop import get_current_partition


class SeasonalDrop(Base):
    __tablename__ = 'seasonal_drops'
    __table_args__ = {'extend_existing': True}

    drop_id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('items.item_id'), index=True)
    player_id = Column(Integer, ForeignKey('players.player_id'), index=True, nullable=False)
    date_added = Column(DateTime, index=True, default=func.now())
    npc_id = Column(Integer, ForeignKey('npc_list.npc_id'), index=True)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    value = Column(Integer)
    quantity = Column(Integer)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    authed = Column(Boolean, default=False)
    used_api = Column(Boolean, default=False)
    hidden = Column(Boolean, default=False, nullable=False, server_default="0")
    partition = Column(Integer, default=get_current_partition, index=True)
    unique_id = Column(String(255), nullable=True)
