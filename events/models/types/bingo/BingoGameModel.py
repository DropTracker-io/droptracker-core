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
    :var allow_diagonal: Whether diagonal lines count for winning
    :var center_free: Whether the center tile is a "free" tile
    :var max_boards_per_team: Maximum number of boards each team can have
    """
    __tablename__ = 'bingo_games'
    
    game_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    individual_boards: Mapped[bool] = mapped_column(Boolean, default=True)
    board_size: Mapped[int] = mapped_column(Integer, default=5)
    win_condition: Mapped[str] = mapped_column(String(50), default='single_line')
    allow_diagonal: Mapped[bool] = mapped_column(Boolean, default=True)
    center_free: Mapped[bool] = mapped_column(Boolean, default=True)
    max_boards_per_team: Mapped[int] = mapped_column(Integer, default=1)

    def __init__(
        self,
        *,
        event_id: int,
        individual_boards: bool = True,
        board_size: int = 5,
        win_condition: str = 'single_line',
        allow_diagonal: bool = True,
        center_free: bool = True,
        max_boards_per_team: int = 1,
        **kwargs
    ) -> None:
        """
        Create a new BingoGameModel instance.
        
        Args:
            event_id: The ID of the event this bingo game belongs to
            individual_boards: Whether each team gets individual boards (default: True)
            board_size: Size of the bingo board, typically 5 for 5x5 (default: 5)
            win_condition: Win condition - 'single_line', 'blackout', 'corners', 'x_pattern' (default: 'single_line')
            allow_diagonal: Whether diagonal lines count for winning (default: True)
            center_free: Whether the center tile is automatically completed (default: True)
            max_boards_per_team: Maximum boards each team can have (default: 1)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            individual_boards=individual_boards,
            board_size=board_size,
            win_condition=win_condition,
            allow_diagonal=allow_diagonal,
            center_free=center_free,
            max_boards_per_team=max_boards_per_team,
            **kwargs
        )

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="bingo_game")

    def is_valid_win_condition(self) -> bool:
        """Check if the win condition is valid."""
        valid_conditions = ['single_line', 'blackout', 'corners', 'x_pattern', 'full_house']
        return self.win_condition in valid_conditions

    def get_required_tiles_for_win(self) -> int:
        """Get the number of tiles required to win based on win condition."""
        if self.win_condition == 'single_line':
            return self.board_size  # 5 tiles for a line
        elif self.win_condition == 'blackout' or self.win_condition == 'full_house':
            return self.board_size * self.board_size  # All 25 tiles
        elif self.win_condition == 'corners':
            return 4  # 4 corner tiles
        elif self.win_condition == 'x_pattern':
            return (self.board_size * 2) - 1  # Both diagonals minus center overlap (9 tiles)
        else:
            return self.board_size  # Default to single line
    
    def should_center_be_free(self, x: int, y: int) -> bool:
        """Check if a position should be automatically completed (free space)."""
        center_pos = self.board_size // 2
        return self.center_free and x == center_pos and y == center_pos 