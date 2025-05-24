from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP, func
from sqlalchemy.orm import relationship
from db.base import Base


class EventShopItem(Base):
    __tablename__ = 'event_items'
    ## Items that can be used by a team in the event

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    cost = Column(Integer, nullable=False, default=0)
    effect = Column(Text, nullable=True)
    effect_long = Column(Text, nullable=True)
    emoji = Column(String(255), nullable=True)
    item_type = Column(String(255), nullable=False)
    cooldown = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP, nullable=False, default=func.now())
    updated_at = Column(TIMESTAMP, nullable=False, default=func.now(), onupdate=func.now())
    
    # Relationships
    event = relationship("EventModel", back_populates="items")
    inventories = relationship("EventTeamInventory", back_populates="item") 