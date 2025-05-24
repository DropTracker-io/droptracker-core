# Event type models package
"""
Event type specific models for the droptracker application.

This package contains event type specific database models:
- BoardGameModel: Board game specific event data
"""

# Import all type models
from .BoardGame import BoardGameModel

# Export all models
__all__ = [
    'BoardGameModel',
] 