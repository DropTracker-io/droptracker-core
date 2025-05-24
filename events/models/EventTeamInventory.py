from sqlalchemy import Column, Integer, ForeignKey, TIMESTAMP, func
from sqlalchemy.orm import relationship
from db.base import Base


class EventTeamInventory(Base):
    __tablename__ = 'event_team_inventory'

    id = Column(Integer, primary_key=True)
    event_team_id = Column(Integer, ForeignKey('event_teams.id'), nullable=False)
    item_id = Column(Integer, ForeignKey('event_items.id'), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # Relationships
    team = relationship("EventTeamModel", back_populates="inventory")
    item = relationship("EventShopItem", back_populates="inventories") 