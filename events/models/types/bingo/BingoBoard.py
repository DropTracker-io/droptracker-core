from sqlalchemy import Integer, ForeignKey
from sqlalchemy.orm import relationship, Mapped, mapped_column
from typing import TYPE_CHECKING, List, Optional
from db.base import Base
from events.generators import BingoBoardGen

# Import types only for type checking to avoid circular imports
if TYPE_CHECKING:
    from ...EventModel import EventModel
    from .BingoBoardTile import BingoBoardTile
    from ...EventTeamModel import EventTeamModel

class BingoBoardModel(Base):
    """
    Represents a bingo board in the database.
    :var board_id: The ID of the bingo board
    :var event_id: The ID of the associated event
    :var team_id: The ID of the team this board belongs to (nullable for shared boards)
    """
    __tablename__ = 'bingo_boards'
    
    board_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey('events.id'))
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey('event_teams.id'), nullable=True)

    def __init__(self, *, event_id: int, team_id: Optional[int] = None, **kwargs) -> None:
        """
        Create a new BingoBoardModel instance.
        
        Args:
            event_id: The ID of the event this bingo board belongs to
            team_id: Optional ID of the team this board belongs to (None for shared boards)
            **kwargs: Additional keyword arguments passed to SQLAlchemy
        """
        super().__init__(
            event_id=event_id,
            team_id=team_id,
            **kwargs
        )

    # Relationships with proper type hints
    event: Mapped["EventModel"] = relationship("EventModel", back_populates="bingo_boards")
    tiles: Mapped[List["BingoBoardTile"]] = relationship("BingoBoardTile", back_populates="board", cascade="all, delete-orphan")
    team: Mapped[Optional["EventTeamModel"]] = relationship("EventTeamModel", back_populates="bingo_boards")

    def get_tile_at_position(self, x: int, y: int) -> "BingoBoardTile | None":
        """Get the tile at the specified position."""
        for tile in self.tiles:
            if tile.position_x == x and tile.position_y == y:
                return tile
        return None

    def is_row_complete(self, row: int) -> bool:
        """Check if a specific row is complete."""
        row_tiles = [tile for tile in self.tiles if tile.position_y == row]
        return all(tile.is_completed() for tile in row_tiles)

    def is_column_complete(self, col: int) -> bool:
        """Check if a specific column is complete."""
        col_tiles = [tile for tile in self.tiles if tile.position_x == col]
        return all(tile.is_completed() for tile in col_tiles)

    def is_diagonal_complete(self, diagonal: str = 'main') -> bool:
        """Check if a diagonal is complete. diagonal can be 'main' or 'anti'."""
        if diagonal == 'main':
            # Main diagonal (top-left to bottom-right)
            diagonal_tiles = [tile for tile in self.tiles if tile.position_x == tile.position_y]
        else:
            # Anti-diagonal (top-right to bottom-left)
            diagonal_tiles = [tile for tile in self.tiles if tile.position_x + tile.position_y == 4]
        
        return all(tile.is_completed() for tile in diagonal_tiles)

    def check_bingo_conditions(self) -> List[str]:
        """Check all possible bingo conditions and return completed ones."""
        completed_conditions = []
        
        # Check rows
        for row in range(5):
            if self.is_row_complete(row):
                completed_conditions.append(f"row_{row}")
        
        # Check columns
        for col in range(5):
            if self.is_column_complete(col):
                completed_conditions.append(f"col_{col}")
        
        # Check diagonals
        if self.is_diagonal_complete('main'):
            completed_conditions.append("diagonal_main")
        if self.is_diagonal_complete('anti'):
            completed_conditions.append("diagonal_anti")
        
        return completed_conditions

    @classmethod
    def create_with_tiles(cls, event_id: int, task_ids: List[int], team_id: Optional[int] = None) -> "BingoBoardModel":
        """
        Create a new bingo board with a 5x5 grid of tiles.
        
        Args:
            event_id: The ID of the event
            task_ids: List of 25 task IDs to assign to the board tiles
            team_id: Optional ID of the team this board belongs to (None for shared boards)
            
        Returns:
            BingoBoardModel: The created board with all tiles
            
        Raises:
            ValueError: If task_ids doesn't contain exactly 25 task IDs
        """
        if len(task_ids) != 25:
            raise ValueError("Bingo board requires exactly 25 task IDs")
        
        # Import here to avoid circular imports
        from .BingoBoardTile import BingoBoardTile
        
        # Create the board
        board = cls(event_id=event_id, team_id=team_id)
        
        # Create tiles for 5x5 grid
        tile_index = 0
        for y in range(5):
            for x in range(5):
                tile = BingoBoardTile(
                    board_id=board.board_id,  # This will be set after board is committed
                    task_id=task_ids[tile_index],
                    position_x=x,
                    position_y=y
                )
                board.tiles.append(tile)
                tile_index += 1
        
        return board

    def generate_board_image(self, cell_size: int = 100, 
                           save_path: Optional[str] = None) -> "BingoBoardGen.BingoBoard":
        """
        Generate a visual board image from the database tiles.
        
        Args:
            cell_size: Size of each cell in the generated image
            save_path: Optional path to save the board state
            
        Returns:
            BingoBoardGen.BingoBoard instance
        """
        from events.generators import BingoBoardGen
        
        # Create the board generator
        board_gen = BingoBoardGen.BingoBoard(
            size=5, 
            cell_size=cell_size, 
            background_color=(255, 255, 255), 
            border_color=(0, 0, 0), 
            border_width=2,
            board_id=self.board_id
        )
        
        # Prepare tile data
        tiles_data = []
        for tile in self.tiles:
            # Access the actual EventTask through the AssignedTask relationship
            event_task = tile.task.task if tile.task else None
            
            tile_data = {
                'position_x': tile.position_x,
                'position_y': tile.position_y,
                'status': tile.status,
                'task': {
                    'name': event_task.name if event_task else 'Unknown Task',
                    'required_items': event_task.required_items if event_task else {}
                }
            }
            tiles_data.append(tile_data)
        
        # Populate the board
        board_gen.populate_from_tasks(tiles_data)
        
        # Save state if requested
        if save_path:
            board_gen.save_board_state(save_path)
        
        return board_gen