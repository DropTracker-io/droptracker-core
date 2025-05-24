from sqlalchemy import Column, Integer, String, Text, ForeignKey, TIMESTAMP, func
from sqlalchemy.orm import relationship
from db.base import Base


class BoardGameModel(Base):
    """
    Represents a board game event in the database.
    :var id: The ID of the board game event
    :var die_sides: The number of sides on the dice
    :var total_tiles: The total number of tiles on the board
    """
    __tablename__ = 'board_game'
    game_id = Column(Integer, primary_key=True)
    die_sides = Column(Integer, default=6)
    total_tiles = Column(Integer, default=100)
    
    event_id = Column(Integer, ForeignKey('events.id'), nullable=False)

    #Relationships
    event = relationship("EventModel", back_populates="board_game")
