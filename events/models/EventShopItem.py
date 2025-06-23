from sqlalchemy import Integer, String, Text, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventModel import EventModel
    from .EventTeamInventory import EventTeamInventory


class EventShopItem(Base):
    """
    Represents an item that can be used by a team in an event.
    :var id: The ID of the item
    :var event_id: The ID of the event
    :var name: The name of the item
    :var description: The description of the item
    :var cost: The cost of the item
    :var effect: The effect of the item
    :var effect_long: The long effect of the item
    :var emoji: The emoji of the item
    :var item_type: The type of the item
    :var cooldown: The cooldown of the item
    :var created_at: The date and time the item was created
    :var updated_at: The date and time the item was last updated
    """
    __tablename__ = 'event_items'
    ## Items that can be used by a team in the event

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cost: Mapped[int] = mapped_column(Integer, default=0)
    effect: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    effect_long: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    emoji: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    item_type: Mapped[str] = mapped_column(String(255))
    cooldown: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        event_id: int,
        name: str,
        item_type: str,
        description: Optional[str] = None,
        cost: int = 0,
        effect: Optional[str] = None,
        effect_long: Optional[str] = None,
        emoji: Optional[str] = None,
        cooldown: int = 0,
        **kwargs
    ) -> None:
        """
        Create a new EventShopItem instance.
        
        Args:
            event_id: The ID of the event
            name: The name of the item
            item_type: The type of the item
            description: Optional description of the item
            cost: The cost of the item (default: 0)
            effect: Optional short effect description
            effect_long: Optional long effect description
            emoji: Optional emoji for the item
            cooldown: Item cooldown in turns (default: 0)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            name=name,
            item_type=item_type,
            description=description,
            cost=cost,
            effect=effect,
            effect_long=effect_long,
            emoji=emoji,
            cooldown=cooldown,
            **kwargs
        )
    
    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="items")
    inventories: Mapped[List["EventTeamInventory"]] = relationship("EventTeamInventory", back_populates="item") 