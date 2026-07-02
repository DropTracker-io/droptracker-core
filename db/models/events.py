"""Events system models (backend Task 14, FRONTEND_PLAN.md Phase 6).

Events with typed tasks, teams, and optional bingo boards. This slice backs the
front-end's focused subset (listing/detail, tasks, teams, read-only bingo).
Effects/cooldowns/shop and the bingo designer are out of scope but the schema
does not preclude them. Scoring is driven by the submission pipeline, not the
web API.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
)
from sqlalchemy import func

from .base import Base

# Must match the contract EVENT_TASK_TYPES.
EVENT_TASK_TYPES = (
    "item_collection",
    "kc_target",
    "xp_target",
    "ehp_target",
    "ehb_target",
    "pb_target",
    "skill_target",
)


class Event(Base):
    # Namespaced under `web_*` to avoid colliding with the pre-existing legacy
    # events system (its `events`/`event_tasks`/`event_teams` tables have a
    # completely different, actively-used schema). This is the web platform's
    # own Phase-6 events feature.
    __tablename__ = "web_events"
    __table_args__ = (
        Index("idx_web_event_group_status", "group_id", "status"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="active")  # draft|active|past
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    has_bingo = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventTask(Base):
    __tablename__ = "web_event_tasks"
    __table_args__ = (Index("idx_web_event_task_event", "event_id"), {"extend_existing": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    type = Column(String(24), nullable=False)
    label = Column(String(255), nullable=False)
    target = Column(String(120), nullable=True)
    target_value = Column(Integer, nullable=True)
    points = Column(Integer, nullable=False, default=0)


class EventTeam(Base):
    __tablename__ = "web_event_teams"
    __table_args__ = (Index("idx_web_event_team_event", "event_id"), {"extend_existing": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    name = Column(String(80), nullable=False)
    score = Column(Integer, nullable=False, default=0)


class EventTeamMember(Base):
    __tablename__ = "web_event_team_members"
    __table_args__ = ({"extend_existing": True},)

    team_id = Column(Integer, ForeignKey("web_event_teams.id"), primary_key=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)


class EventBingoCell(Base):
    __tablename__ = "web_event_bingo_cells"
    __table_args__ = (Index("idx_web_bingo_cell_event", "event_id"), {"extend_existing": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    idx = Column(Integer, nullable=False)
    label = Column(String(255), nullable=False)
    task_id = Column(Integer, ForeignKey("web_event_tasks.id"), nullable=True)


class EventBingoCompletion(Base):
    __tablename__ = "web_event_bingo_completions"
    __table_args__ = (Index("idx_web_bingo_completion_cell", "cell_id"), {"extend_existing": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    cell_id = Column(Integer, ForeignKey("web_event_bingo_cells.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=True)
