"""boardgen — procedural OSRS-style hex board generator (layered SVG output)."""
from .board import Board, Tile, Region
from .render import render

__all__ = ["Board", "Tile", "Region", "render"]
__version__ = "0.1.0"
