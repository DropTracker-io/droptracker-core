from sqlalchemy import Boolean, Integer, ForeignKey, String
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import TYPE_CHECKING
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from ..EventModel import EventModel


class BingoGameModel(Base):
    """
    Represents a bingo game event in the database.
    :var game_id: The ID of the bingo game event
    :var individual_boards: Whether or not each team is assigned their own board
    :var board_size: The size of the board (takes one number, 5 = 5x5, etc.)
    :var win_condition: The win condition of the game (supported: "blackout", "line", "corners", "x")
    :var event_id: The ID of the associated event
    """
    __tablename__ = 'bingo_game'
    
    game_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    individual_boards: Mapped[bool] = mapped_column(Boolean, default=False)
    board_size: Mapped[int] = mapped_column(Integer, default=5)
    win_condition: Mapped[str] = mapped_column(String(255), default="blackout")
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="bingo_game")


    def __init__(self, *, event_id: int, individual_boards: bool = False, board_size: int = 5, win_condition: str = "blackout", **kwargs) -> None:
        """
        Create a new BoardGameModel instance.
        :var event_id: The ID of the event
        :var individual_boards: Whether or not each team is assigned their own board
        :var board_size: The size of the board (takes one number, 5 = 5x5, etc.)
        :var win_condition: The win condition of the game (supported: "blackout", "line", "corners", "x")
        """
        super().__init__(
            event_id=event_id,
            individual_boards=individual_boards,
            board_size=board_size,
            win_condition=win_condition,
            **kwargs
        )