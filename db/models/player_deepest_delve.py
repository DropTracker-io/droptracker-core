"""Deepest Doom of Mokhaiotl delve level each player has completed (web124a).

Written by the PB intake (utils/doom_delve.record_completed), read by the Hall
of Fame's ``delve`` board. A watermark: it only moves up. ``exact`` = 0 means
"some level past 8" with the real level unknown (shown as "9+").
"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, func

from .base import Base


class PlayerDeepestDelve(Base):
    __tablename__ = "player_deepest_delve"
    __table_args__ = (
        Index("ix_player_deepest_delve_level", "deepest_level"),
        {"extend_existing": True},
    )

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    deepest_level = Column(Integer, nullable=False)
    exact = Column(Boolean, nullable=False, default=True)
    achieved_at = Column(DateTime, nullable=False)
    updated_at = Column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
