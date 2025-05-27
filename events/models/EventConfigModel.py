from sqlalchemy import Integer, String, Text, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventModel import EventModel


class EventConfigModel(Base):
    """
    Represents a configuration for an event in the database.
    :var id: The ID of the configuration
    :var event_id: The ID of the event
    :var config_key: The key of the configuration
    :var config_value: The value of the configuration
    :var long_value: The long value of the configuration
    :var update_number: The number of updates to the configuration
    :var created_at: The date and time the configuration was created
    :var updated_at: The date and time the configuration was last updated
    """
    __tablename__ = 'event_configurations'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    config_key: Mapped[str] = mapped_column(String(255))
    config_value: Mapped[str] = mapped_column(String(255))
    long_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    update_number: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        event_id: int,
        config_key: str,
        config_value: str,
        long_value: Optional[str] = None,
        update_number: int = 0,
        **kwargs
    ) -> None:
        """
        Create a new EventConfigModel instance.
        
        Args:
            event_id: The ID of the event
            config_key: The configuration key
            config_value: The configuration value
            long_value: Optional long text value
            update_number: Number of updates to this configuration (default: 0)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            config_key=config_key,
            config_value=config_value,
            long_value=long_value,
            update_number=update_number,
            **kwargs
        )
    
    # Relationship with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="configurations") 