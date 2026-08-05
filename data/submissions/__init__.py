"""Submissions package facade.

This package provides backward-compatible imports for the former
`data.submissions` module by re-exporting processor functions and
utilities. External callers can continue using

    from data.submissions import drop_processor, pb_processor, ...

without any code changes.

Modules:
- common: shared state and utilities used across processors
- drop: drop submission processor
- pb: personal best processor
- ca: combat achievements processor
- clog: collection log processor
- pet: pet processor
- adventure_log: adventure log processor
"""

# Re-export processors for backward compatibility
from .clan_broadcast import clan_broadcast_processor  # noqa: F401
from .clan_chat import clan_chat_processor  # noqa: F401
from .drop import drop_processor  # noqa: F401
from .pb import pb_processor  # noqa: F401
from .ca import ca_processor  # noqa: F401
from .clog import clog_processor  # noqa: F401
from .pet import pet_processor  # noqa: F401
from .adventure_log import adventure_log_processor  # noqa: F401
from .experience import experience_processor  # noqa: F401
from .quest import quest_processor  # noqa: F401
from .death import death_processor  # noqa: F401
from .diary import diary_processor  # noqa: F401

# Utilities used externally
from .common import (
    SubmissionResponse,
    RawDropData,
    try_create_player,
)

__all__ = [
    "clan_broadcast_processor",
    "clan_chat_processor",
    "drop_processor",
    "pb_processor",
    "ca_processor",
    "clog_processor",
    "pet_processor",
    "adventure_log_processor",
    "experience_processor",
    "quest_processor",
    "death_processor",
    "diary_processor",
    "SubmissionResponse",
    "RawDropData",
    "try_create_player",
]


