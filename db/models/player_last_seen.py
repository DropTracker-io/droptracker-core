"""When each player's DropTracker plugin was last seen talking to us.

One row per player, written by :mod:`utils.player_last_seen` from the
submission path (both transports) and the event notifications poll. Writes
are throttled per account, so the value is accurate to within
``utils.player_last_seen.CLAIM_TTL_SECONDS``. Read by the data API's
``identity.last_seen``.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer

from .base import Base


class PlayerLastSeen(Base):
    __tablename__ = "player_last_seen"
    __table_args__ = ({"extend_existing": True},)

    # CASCADE: a record about the player must never be what stops one being
    # removed.
    player_id = Column(
        Integer, ForeignKey("players.player_id", ondelete="CASCADE"), primary_key=True
    )
    last_seen_at = Column(DateTime, nullable=False)
