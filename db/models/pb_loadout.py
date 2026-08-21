"""What a player was wearing and carrying when they set a personal best.

A separate table rather than columns on ``personal_best`` for two reasons: the
data is optional (older clients and opted-out players send none, so most rows
would be null), and ``personal_best`` is a large, hot table that should not take
an ALTER for an additive feature.

Stored decoded as JSON rather than in the plugin's compact wire format so it is
readable in a database client and queryable later — "what gear is meta for this
boss" is the interesting question this makes possible, and nothing else in the
ecosystem can answer it.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text, func

from .base import Base


class PersonalBestLoadout(Base):
    __tablename__ = "personal_best_loadouts"
    __table_args__ = ({"extend_existing": True},)

    # One loadout per personal best entry, so the PB id is the key outright.
    pb_id = Column(Integer, ForeignKey("personal_best.id"), primary_key=True)
    # JSON arrays of {"slot": int, "item_id": int, "quantity": int}.
    # Null means the client sent nothing; an empty array would wrongly imply the
    # player was wearing or carrying nothing at all.
    equipment = Column(Text, nullable=True)
    inventory = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
