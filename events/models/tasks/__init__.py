# Task models package
"""
Task-related models and utilities for the event system.

This module provides:
- EventTask: Core task model with support for different task types
- BaseTask: Template tasks that can be reused across events
- AssignedTask: Task assignment and progress tracking
- TaskType: Enumeration of supported task types
- TaskFactory: Factory methods for creating different types of tasks
- TrackedTaskData: Data tracking for task progress
"""

# Import all task models
from .EventTask import EventTask, TaskType
from .BaseTask import BaseTask
from .AssignedTask import AssignedTask
from .TaskFactory import TaskFactory
from .data import TrackedTaskData

# Export all models
__all__ = [
    'EventTask',
    'BaseTask',
    'TaskType',
    'AssignedTask',
    'TaskFactory',
    'TrackedTaskData'
] 