"""Events system models (backend Task 14, FRONTEND_PLAN.md Phase 6).

Events with typed tasks, teams, and optional bingo boards. This slice backs the
front-end's focused subset (listing/detail, tasks, teams, read-only bingo).
Effects/cooldowns/shop and the bingo designer are out of scope but the schema
does not preclude them. Scoring is driven by the submission pipeline, not the
web API.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
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
    "loot_value",
    # Manual-confirmation-only tasks (no automated evaluation).
    "custom",
)

EVENT_FORMATION_MODES = ("self_join", "auto_assign", "admin_assign")

# Ledger row lifecycle for web_event_completions.
EVENT_COMPLETION_STATUSES = ("auto", "pending", "confirmed", "rejected", "manual", "revoked")

# Per-event Discord destination kinds (web_event_channels.kind).
EVENT_CHANNEL_KINDS = ("announcements", "completions", "leaderboard", "admin")

EVENT_BOARD_SIZES = (3, 4, 5, 6, 7)  # square boards; default 5


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
    # --- schema v2 (Task 15) ---
    formation_mode = Column(String(16), nullable=False, default="admin_assign")  # EVENT_FORMATION_MODES
    requires_confirmation = Column(Boolean, nullable=False, default=False)  # event-level force (PRD D3)
    join_code = Column(String(32), nullable=True)  # optional self-join code; never in public reads
    discord_guild_id = Column(String(32), nullable=True)  # snowflake; any guild the bot is in (PRD D8)
    board_size = Column(Integer, nullable=False, default=5)  # EVENT_BOARD_SIZES
    bonus_line_points = Column(Integer, nullable=False, default=0)
    bonus_blackout_points = Column(Integer, nullable=False, default=0)
    activated_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
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
    requires_confirmation = Column(Boolean, nullable=False, default=False)  # per-task flag (PRD D3)
    config = Column(Text, nullable=True)  # JSON: any_of/assembly/point_collection item lists etc.


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
    # Credit cutoff: the engine ignores submissions timestamped earlier (PRD D10).
    joined_at = Column(DateTime, default=func.now(), nullable=False)


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


class EventCompletion(Base):
    """Per-action ledger (schema v2): one row per qualifying submission or
    manual admin action. Confirmation status lives here; the rollup that
    drives completion/points is ``EventProgress``."""

    __tablename__ = "web_event_completions"
    __table_args__ = (
        Index("idx_web_evt_completion_event", "event_id", "status"),
        Index("idx_web_evt_completion_task_team", "task_id", "team_id"),
        # Queue-replay idempotency; NULL guids (manual/bonus rows) are exempt.
        Index("uq_web_evt_completion_src", "task_id", "team_id", "submission_guid", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("web_event_tasks.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=True)
    status = Column(String(16), nullable=False)  # EVENT_COMPLETION_STATUSES
    quantity = Column(Integer, nullable=False, default=1)
    source_type = Column(String(16), nullable=True)  # drop|pb|clog|ca|experience|manual|bonus
    source_id = Column(BigInteger, nullable=True)
    submission_guid = Column(String(64), nullable=True)
    proof_url = Column(String(255), nullable=True)
    # Canonical item name this row credited (item_collection matches). Drives
    # distinct-item progress for all_of/assembly tasks — a 1,338-coins drop is
    # still just "Coins". NULL on non-item rows and manual wildcard awards.
    matched_target = Column(String(120), nullable=True)
    acted_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventProgress(Base):
    """Rollup, one row per (task, team), folded from non-pending, non-revoked
    ledger rows by the event consumer / shared engine service."""

    __tablename__ = "web_event_progress"
    __table_args__ = (
        Index("uq_web_evt_progress", "task_id", "team_id", unique=True),
        Index("idx_web_evt_progress_event", "event_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("web_event_tasks.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=False)
    progress = Column(Integer, nullable=False, default=0)
    completed = Column(Boolean, nullable=False, default=False)
    completed_at = Column(DateTime, nullable=True)


class EventChannel(Base):
    """Per-event Discord destination (PRD D8)."""

    __tablename__ = "web_event_channels"
    __table_args__ = (
        Index("uq_web_event_channel", "event_id", "kind", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    kind = Column(String(24), nullable=False)  # EVENT_CHANNEL_KINDS
    channel_id = Column(String(32), nullable=False)


class EventTaskLibraryItem(Base):
    """Curated task presets, seeded from the legacy BoardGame task store
    (``games/events/task_store/default.json``) by
    ``scripts/seed_event_task_library.py``."""

    __tablename__ = "web_event_task_library"
    __table_args__ = (
        Index("uq_web_evt_library_name_source", "name", "source", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    type = Column(String(24), nullable=False)  # EVENT_TASK_TYPES
    target = Column(String(120), nullable=True)
    target_value = Column(Integer, nullable=True)
    default_points = Column(Integer, nullable=False, default=0)
    difficulty = Column(String(24), nullable=True)  # air|water|earth|fire (legacy tiers)
    config = Column(Text, nullable=True)  # JSON: item lists / semantics
    source = Column(String(24), nullable=False, default="legacy_v1")
    active = Column(Boolean, nullable=False, default=True)
