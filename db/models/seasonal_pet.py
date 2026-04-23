from sqlalchemy import Column, Integer, String, ForeignKey, DateTime

from .base import Base


class SeasonalPlayerPet(Base):
    __tablename__ = 'seasonal_player_pets'
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey('players.player_id'))
    item_id = Column(Integer, ForeignKey('items.item_id'))
    date_added = Column(DateTime, nullable=True)
    unique_id = Column(String(255), nullable=True)
    pet_name = Column(String(255), nullable=False)
