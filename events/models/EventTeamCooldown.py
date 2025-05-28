from sqlalchemy import Integer, String, ForeignKey, DateTime, func, text
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .EventTeamModel import EventTeamModel


class EventTeamCooldown(Base):
    """
    Represents a cooldown for a team in an event.
    :var id: The ID of the cooldown
    :var team_id: The ID of the team
    :var cooldown_name: The name of the cooldown
    :var remaining_turns: The remaining turns of the cooldown
    :var expiry_date: The expiry date of the cooldown
    :var created_at: The date and time the cooldown was created
    :var updated_at: The date and time the cooldown was last updated
    """
    __tablename__ = 'event_team_cooldowns'
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey('event_teams.id', ondelete='CASCADE'))
    cooldown_name: Mapped[str] = mapped_column(String(255))
    remaining_turns: Mapped[int] = mapped_column(Integer)
    expiry_date: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    
    def __init__(
        self,
        *,
        team_id: int,
        cooldown_name: str,
        remaining_turns: int,
        expiry_date: datetime,
        **kwargs
    ) -> None:
        """
        Create a new EventTeamCooldown instance.
        
        Args:
            team_id: The ID of the team
            cooldown_name: The name of the cooldown
            remaining_turns: The remaining turns of the cooldown
            expiry_date: The expiry date of the cooldown
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            team_id=team_id,
            cooldown_name=cooldown_name,
            remaining_turns=remaining_turns,
            expiry_date=expiry_date,
            **kwargs
        )
    
    # Relationship with proper type hints
    team: Mapped["EventTeamModel"] = relationship("EventTeamModel", back_populates="cooldowns") 