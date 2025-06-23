from sqlalchemy import Integer, ForeignKey
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import TYPE_CHECKING
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from ..EventModel import EventModel


class BoardGameModel(Base):
    """
    Represents a board game event in the database.
    :var game_id: The ID of the board game event
    :var die_sides: The number of sides on the dice
    :var total_tiles: The total number of tiles on the board
    :var event_id: The ID of the associated event
    """
    __tablename__ = 'board_game'
    
    game_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    die_sides: Mapped[int] = mapped_column(Integer, default=6)
    total_tiles: Mapped[int] = mapped_column(Integer, default=100)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="board_game")


    def __init__(self, *, event_id: int, die_sides: int = 6, total_tiles: int = 100, **kwargs) -> None:
        """
        Create a new BoardGameModel instance.
        :var event_id: The ID of the event
        :var die_sides: The number of sides on the dice
        :var total_tiles: The total number of tiles on the board
        """
        super().__init__(
            event_id=event_id,
            die_sides=die_sides,
            total_tiles=total_tiles,
            **kwargs
        )