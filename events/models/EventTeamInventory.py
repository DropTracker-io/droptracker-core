from sqlalchemy import Integer, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventTeamModel import EventTeamModel
    from .EventShopItem import EventShopItem


class EventTeamInventory(Base):
    """
    Represents inventory items for event teams.
    :var id: The ID of the inventory entry
    :var event_team_id: The ID of the team
    :var item_id: The ID of the item
    :var quantity: The quantity of the item
    :var created_at: When the inventory entry was created
    :var updated_at: When the inventory entry was last updated
    """
    __tablename__ = 'event_team_inventory'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_team_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_teams.id'))
    item_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_items.id'))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        event_team_id: int,
        item_id: int,
        quantity: int = 1,
        **kwargs
    ) -> None:
        """
        Create a new EventTeamInventory instance.
        
        Args:
            event_team_id: The ID of the team
            item_id: The ID of the item
            quantity: The quantity of the item (default: 1)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_team_id=event_team_id,
            item_id=item_id,
            quantity=quantity,
            **kwargs
        )
    
    # Relationships with proper type hints
    team: Mapped["EventTeamModel"] = relationship("EventTeamModel", back_populates="inventory")
    item: Mapped["EventShopItem"] = relationship("EventShopItem", back_populates="inventories") 