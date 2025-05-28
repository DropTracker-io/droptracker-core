from sqlalchemy import Integer, String, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import Optional, TYPE_CHECKING
from datetime import datetime
from db.base import Base

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from .BingoBoard import BingoBoardModel
    from ...tasks.AssignedTask import AssignedTask
    from ...EventTeamModel import EventTeamModel


class BingoBoardTile(Base):
    """
    Represents a single tile on a bingo board.
    :var tile_id: The ID of the tile
    :var board_id: The ID of the associated bingo board
    :var task_id: The ID of the task assigned to this tile
    :var position_x: X coordinate on the bingo board (0-4)
    :var position_y: Y coordinate on the bingo board (0-4)
    :var status: Status of the tile ('pending', 'completed', 'claimed')
    :var completed_by_team_id: ID of the team that completed this tile
    :var date_completed: When the tile was completed
    :var created_at: When the tile was created
    :var updated_at: When the tile was last updated
    """
    __tablename__ = 'bingo_board_tiles'
    
    tile_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    board_id: Mapped[int] = mapped_column(Integer, ForeignKey('bingo_boards.board_id'))
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey('assigned_tasks.id'))
    position_x: Mapped[int] = mapped_column(Integer)  # 0-4 for 5x5 board
    position_y: Mapped[int] = mapped_column(Integer)  # 0-4 for 5x5 board
    status: Mapped[str] = mapped_column(String(50), default='pending')
    completed_by_team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey('event_teams.id'), nullable=True)
    date_completed: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    def __init__(
        self,
        *,
        board_id: int,
        task_id: int,
        position_x: int,
        position_y: int,
        status: str = 'pending',
        completed_by_team_id: Optional[int] = None,
        date_completed: Optional[datetime] = None,
        **kwargs
    ) -> None:
        """
        Create a new BingoBoardTile instance.
        
        Args:
            board_id: The ID of the bingo board this tile belongs to
            task_id: The ID of the task assigned to this tile
            position_x: X coordinate on the board (0-4)
            position_y: Y coordinate on the board (0-4)
            status: Status of the tile (default: 'pending')
            completed_by_team_id: Optional ID of the team that completed this tile
            date_completed: Optional completion date
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            board_id=board_id,
            task_id=task_id,
            position_x=position_x,
            position_y=position_y,
            status=status,
            completed_by_team_id=completed_by_team_id,
            date_completed=date_completed,
            **kwargs
        )

    # Relationships with proper type hints
    board: Mapped["BingoBoardModel"] = relationship("BingoBoardModel", back_populates="tiles")
    task: Mapped["AssignedTask"] = relationship("AssignedTask")
    completed_by_team: Mapped[Optional["EventTeamModel"]] = relationship("EventTeamModel")

    def mark_completed(self, team_id: int) -> None:
        """Mark this tile as completed by a specific team."""
        self.status = 'completed'
        self.completed_by_team_id = team_id
        self.date_completed = datetime.now()

    def is_completed(self) -> bool:
        """Check if this tile is completed."""
        return self.status == 'completed'

    def get_position(self) -> tuple[int, int]:
        """Get the position of this tile as a tuple (x, y)."""
        return (self.position_x, self.position_y)
