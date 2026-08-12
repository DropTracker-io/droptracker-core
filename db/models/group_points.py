from sqlalchemy import Boolean, Column, Integer, DateTime, ForeignKey, String, Text
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base


class PlayerPoints(Base):
    __tablename__ = 'player_points'
    __table_args__ = {
        'extend_existing': True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey('players.player_id'), nullable=False)
    group_id = Column(Integer, nullable=True)
    amount = Column[int](Integer, nullable=False, default=0)
    reason = Column(String(125), nullable=False, default="")
    entry_type = Column[int](Integer, nullable=True)
    entry_id = Column[int](Integer, nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    
    # Relationships
    player = relationship("Player", back_populates="player_points")

class GroupPointConfig(Base):
    __tablename__ = 'group_point_settings'
    __table_args__ = {
        'extend_existing': True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, nullable=True)
    reason = Column[str](String(125), nullable=False, default="any")
    award = Column[int](Integer, nullable=False, default=1)
    divisor = Column[int](Integer, nullable=False, default=1)
    can_modify = Column[bool](Boolean, nullable=False, default=False)
    description = Column[str](String(255), nullable=True)

class GroupPointMods(Base):
    __tablename__ = 'group_point_mods'
    __table_args__ = {
        'extend_existing': True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, nullable=True)
    item_id = Column[int](Integer, nullable=True)
    npc_id = Column[int](Integer, nullable=True)
    event_type = Column[str](String(125), nullable=False, default="any")
    award = Column[int](Integer, nullable=False, default=1)
    divisor = Column[int](Integer, nullable=False, default=1)
    can_modify = Column[bool](Boolean, nullable=False, default=True)
    description = Column[str](String(255), nullable=True)

class GroupPointTimedEvent(Base):
    __tablename__ = 'group_point_events'
    __table_args__ = {
        'extend_existing': True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=True)
    start_time_unix = Column[int](Integer, nullable=False)
    end_time_unix = Column[int](Integer, nullable=False)
    # Which submission reason this event applies to ("drop", "pb", etc). Use "any" for all.
    event_type = Column[str](String(125), nullable=False, default="any")

    # Target selection:
    # - target_type: "any", "item", or "npc"
    # - target_id: specific item_id/npc_id, or NULL/0 to apply to any of that type
    # - target_ids: JSON int array for multi-target boosts; when set it wins
    #   over target_id (which stays NULL on multi-target rows)
    target_type = Column[str](String(125), nullable=False, default="any")
    target_id = Column[int](Integer, nullable=True)
    target_ids = Column[str](Text, nullable=True)

    # Operation to apply to the computed points: "multiply", "add", "set",
    # or "add_per_member" (total bonus = value × in-group participants present,
    # receiver included; with sharing on each member's share gets +value once)
    operation = Column[str](String(125), nullable=False, default="multiply")
    operation_value = Column[int](Integer, nullable=False, default=1)
    description = Column[str](String(255), nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    
    # Relationships
    group = relationship("Group", back_populates="group_point_timed_events")

class GroupPointSeason(Base):
    """Admin-defined leaderboard window (e.g. a recruitment drive or quarterly comp)."""
    __tablename__ = 'group_point_seasons'
    __table_args__ = {
        'extend_existing': True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=False)
    name = Column[str](String(100), nullable=False)
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, onupdate=func.now(), default=func.now())


class GroupPointBlacklist(Base):
    __tablename__ = 'group_point_blacklist'
    __table_args__ = {
        'extend_existing': True,
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    list_type = Column[str](String(10), nullable=False, default="whitelist")
    group_id = Column(Integer, ForeignKey('groups.group_id'), nullable=True)
    item_id = Column[int](Integer, nullable=True)
    npc_id = Column[int](Integer, nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    
    # Relationships
    group = relationship("Group", back_populates="group_point_blacklist")