# Event type models package
"""
Event type specific models for the droptracker application.

This package contains event type specific database models:
- BoardGameModel: Board game specific event data
- BingoBoardModel: Bingo board model for bingo events
- BingoBoardTile: Individual tiles on bingo boards
"""

# Import all type models
from .BoardGame import BoardGameModel
from .bingo import BingoBoardModel, BingoBoardTile

# Export all models
__all__ = [
    'BoardGameModel',
    'BingoBoardModel',
    'BingoBoardTile',
    'BingoGameModel',
] 