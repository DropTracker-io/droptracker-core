from sqlalchemy import Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, TYPE_CHECKING
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from ...EventModel import EventModel


class BingoGameModel(Base):
    """
    Represents bingo game configuration for an event.
    :var game_id: The ID of the bingo game configuration
    :var event_id: The ID of the associated event
    :var individual_boards: Whether each team gets individual boards or shares one
    :var board_size: Size of the bingo board (typically 5 for 5x5)
    :var win_condition: How teams can win ('single_line', 'blackout', 'corners', 'x_pattern')
    :var center_free: Whether the center tile is a "free" tile
    """
    __tablename__ = 'bingo_games'
    
    game_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    individual_boards: Mapped[bool] = mapped_column(Boolean, default=True)
    board_size: Mapped[int] = mapped_column(Integer, default=5)
    win_condition: Mapped[str] = mapped_column(String(50), default='single_line')   
    center_free: Mapped[bool] = mapped_column(Boolean, default=True)

    def __init__(
        self,
        *,
        event_id: int,
        individual_boards: bool = True,
        board_size: int = 5,
        win_condition: str = 'single_line',
        center_free: bool = True,
        **kwargs
    ) -> None:
        """
        Create a new BingoGameModel instance.
        
        Args:
            event_id: The ID of the event this bingo game belongs to
            individual_boards: Whether each team gets individual boards (default: True)
            board_size: Size of the bingo board, typically 5 for 5x5 (default: 5)
            win_condition: Win condition - 'single_line', 'blackout', 'corners', 'x_pattern' (default: 'single_line')
            center_free: Whether the center tile is automatically completed (default: True)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            individual_boards=individual_boards,
            board_size=board_size,
            win_condition=win_condition,
            center_free=center_free,
            **kwargs
        )

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="bingo_game")

    def is_valid_win_condition(self) -> bool:
        """Check if the win condition is valid."""
        valid_conditions = ['single_line', 'blackout', 'corners', 'x_pattern', 'full_house']
        return self.win_condition in valid_conditions

    def should_center_be_free(self, x: int, y: int) -> bool:
        """Check if a position should be automatically completed (free space)."""
        center_pos = self.board_size // 2
        return self.center_free and x == center_pos and y == center_pos 