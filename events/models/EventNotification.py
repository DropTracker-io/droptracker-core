from sqlalchemy import Integer, String, Text, ForeignKey, DateTime, Boolean, UniqueConstraint, Column
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventModel import EventModel
    from db.models import Group


class EventNotification(Base):
    """
    Represents a notification for an event in the database.
    :var id: The ID of the notification
    :var event_id: The ID of the event
    :var notification_type: The type of notification
    :var group_id: The ID of the player group
    :var message: The notification message
    :var data: Additional JSON data for the notification
    :var created_at: The date and time the notification was created
    :var processed_at: The date and time the notification was processed
    :var status: The status of the notification
    :var error_message: Any error message if the notification failed
    """
    __tablename__ = 'event_notifications'
    __table_args__ = (
        UniqueConstraint('event_id', 'notification_type', 'group_id', 'data', 
                        name='uix_event_notification_unique'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(50), nullable=False)  
    group_id: Mapped[int] = mapped_column(Integer, ForeignKey('groups.group_id'), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON data
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='pending', nullable=False)  # 'pending', 'processing', 'sent', 'failed'
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    def __init__(
        self,
        *,
        event_id: int,
        notification_type: str,
        group_id: int,
        message: str,
        data: Optional[str] = None,
        status: str = 'pending',
        error_message: Optional[str] = None,
        **kwargs
    ) -> None:
        """
        Create a new EventNotification instance.
        
        Args:
            event_id: The ID of the event
            notification_type: The type of notification
            group_id: The ID of the player group
            message: The notification message
            data: Optional JSON data for the notification
            status: The status of the notification (default: 'pending')
            error_message: Optional error message if the notification failed
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            notification_type=notification_type,
            group_id=group_id,
            message=message,
            data=data,
            status=status,
            error_message=error_message,
            **kwargs
        )
    
    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="notifications")