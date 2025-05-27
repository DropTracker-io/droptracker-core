from sqlalchemy import Integer, String, Text, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventTeamModel import EventTeamModel


class EventTeamEffect(Base):
    """
    Represents an effect applied to a team in an event.
    :var id: The ID of the effect
    :var team_id: The ID of the team
    :var effect_name: The name of the effect
    :var remaining_turns: The remaining turns of the effect
    :var expiry_date: The expiry date of the effect
    :var effect_data: Additional effect data as JSON
    :var created_at: The date and time the effect was created
    :var updated_at: The date and time the effect was last updated
    """
    __tablename__ = 'event_team_effects'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_teams.id', ondelete='CASCADE'))
    effect_name: Mapped[str] = mapped_column(String(255))
    remaining_turns: Mapped[int] = mapped_column(Integer)
    expiry_date: Mapped[datetime] = mapped_column(DateTime)
    effect_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # For any additional effect data as JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        team_id: int,
        effect_name: str,
        remaining_turns: int,
        expiry_date: datetime,
        effect_data: Optional[str] = None,
        **kwargs
    ) -> None:
        """
        Create a new EventTeamEffect instance.
        
        Args:
            team_id: The ID of the team
            effect_name: The name of the effect
            remaining_turns: The remaining turns of the effect
            expiry_date: The expiry date of the effect
            effect_data: Optional additional effect data as JSON string
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            team_id=team_id,
            effect_name=effect_name,
            remaining_turns=remaining_turns,
            expiry_date=expiry_date,
            effect_data=effect_data,
            **kwargs
        )
    
    # Relationship with proper type hints
    team: Mapped["EventTeamModel"] = relationship("EventTeamModel", back_populates="effects") 