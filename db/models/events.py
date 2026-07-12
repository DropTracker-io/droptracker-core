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

# Task-library sharing (web_event_tasks.visibility / web_event_task_library.visibility):
# every task an admin creates is saved to the reusable task library — "public"
# rows show up in every group's picker, "private" rows only in the owning
# group's own picker (saved for their future events).
EVENT_TASK_VISIBILITIES = ("public", "private")

# Event ownership shape (web_events.mode): "standard" (one owning group, or
# global when group_id is NULL) vs "clan_vs_clan" (a host group plus invited
# opponent groups, tracked in web_event_groups).
EVENT_MODES = ("standard", "clan_vs_clan")

# web_event_groups participant roles / invite lifecycle.
EVENT_GROUP_ROLES = ("host", "opponent")
EVENT_GROUP_STATUSES = ("invited", "accepted", "declined")

# Ledger row lifecycle for web_event_completions.
EVENT_COMPLETION_STATUSES = ("auto", "pending", "confirmed", "rejected", "manual", "revoked")

# Per-event Discord destination kinds (web_event_channels.kind).
EVENT_CHANNEL_KINDS = ("announcements", "completions", "leaderboard", "admin")

EVENT_BOARD_SIZES = (3, 4, 5, 6, 7)  # square boards; default 5

# Which intake paths may drive automatic task progress (web_events.submission_policy):
# - "all"             — every processed submission counts (API + webhook fallback).
# - "confirm_non_api" — non-API submissions count but always land as pending
#                       ledger rows needing admin confirmation.
# - "api_only"        — submissions without the plugin API flag are ignored.
EVENT_SUBMISSION_POLICIES = ("all", "confirm_non_api", "api_only")

# When the mirrored Discord scheduled event is created (web_events.discord_event_policy):
# - "on_activate" — nothing is created while the event is a draft; the mirror
#                   goes live when the event activates (default: drafts stay
#                   invisible on Discord).
# - "immediate"   — the mirror is created as soon as the event has a guild and
#                   a future start, even while still a draft.
EVENT_DISCORD_POLICIES = ("on_activate", "immediate")

# Ping keys accepted in web_events.ping_config (JSON: {key: [role ids]}).
# - "event_created" — companion message when the Discord scheduled event is
#                     created in the event's primary guild (the main ask:
#                     scheduled events can't ping by themselves).
# - "event_started"/"event_ended" — the lifecycle announcements.
EVENT_PING_KEYS = ("event_created", "event_started", "event_ended")


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
    submission_policy = Column(String(16), nullable=False, default="all")  # EVENT_SUBMISSION_POLICIES
    join_code = Column(String(32), nullable=True)  # optional self-join code; never in public reads
    discord_guild_id = Column(String(32), nullable=True)  # snowflake; any guild the bot is in (PRD D8)
    # EVENT_MODES. clan_vs_clan keeps group_id = the HOST group, so group
    # indexes, the default Discord guild, and entitlement gating work unchanged.
    mode = Column(String(16), nullable=False, default="standard", server_default="standard")
    # EVENT_DISCORD_POLICIES: when the Discord scheduled-event mirror goes
    # live — on activation (default; drafts create nothing on Discord) or
    # immediately at creation.
    discord_event_policy = Column(
        String(16), nullable=False, default="on_activate", server_default="on_activate"
    )
    # JSON {ping_key: [role snowflakes]} (EVENT_PING_KEYS) — which roles the
    # bot mentions on the scheduled-event companion message / lifecycle posts.
    ping_config = Column(Text, nullable=True)
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
    # EVENT_TASK_VISIBILITIES — how the task's library copy is shared: public
    # (any group may reuse it) or private (owning group only).
    visibility = Column(String(16), nullable=False, default="public", server_default="public")


class EventTeam(Base):
    __tablename__ = "web_event_teams"
    __table_args__ = (Index("idx_web_event_team_event", "event_id"), {"extend_existing": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    name = Column(String(80), nullable=False)
    score = Column(Integer, nullable=False, default=0)
    # The clan this team represents (clan_vs_clan only; NULL on standard/global).
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)


class EventGroup(Base):
    """A clan participating in a clan-vs-clan event (web_events.mode).

    Standard/global events write NO rows here — ``Event.group_id`` remains
    their sole owner. For clan-vs-clan the host group is seeded as an
    ``accepted`` row at create time and opponents move invited -> accepted /
    declined via the web UI."""

    __tablename__ = "web_event_groups"
    __table_args__ = (
        Index("uq_web_event_group_part", "event_id", "group_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    role = Column(String(16), nullable=False, default="opponent")   # EVENT_GROUP_ROLES
    status = Column(String(16), nullable=False, default="invited")  # EVENT_GROUP_STATUSES
    invited_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    responded_at = Column(DateTime, nullable=True)
    # Opt-in (accept-time checkbox): mirror the Discord scheduled event into
    # this clan's own linked guild too. Off by default — accepting an invite
    # must not create anything in the accepting clan's server unasked.
    mirror_discord_event = Column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at = Column(DateTime, default=func.now(), nullable=False)


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


class EventGuild(Base):
    """Per-(event, guild) Discord scheduled-event sync state (dual-guild ready).

    The Web API only writes *desired* state here and never talks to Discord
    (``services/event_scheduled_events.py``); the core bot's
    ``reconcile_event_scheduled_events`` task (bots/main.py) creates/edits/
    deletes the real Discord scheduled event and writes back
    ``discord_scheduled_event_id``. Idempotent: a row that already has an id
    is edited, never re-created. One row per guild keeps this ready for
    clan-vs-clan events advertising in multiple guilds."""

    __tablename__ = "web_event_guilds"
    __table_args__ = (
        Index("uq_web_event_guild", "event_id", "guild_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    guild_id = Column(String(32), nullable=False)  # snowflake
    discord_scheduled_event_id = Column(String(32), nullable=True)  # written back by the bot
    sync_status = Column(String(16), nullable=False, default="pending")  # pending|synced|delete_pending|failed
    synced_at = Column(DateTime, nullable=True)
    last_error = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventTaskLibraryItem(Base):
    """Reusable task presets backing the task pickers.

    Two kinds of rows share the table: curated presets seeded from the legacy
    BoardGame task store (``source='legacy_v1'``, ``group_id`` NULL, always
    public) and group-saved tasks (``source='group'``) written whenever an
    admin creates/edits an event task — shared with everyone when
    ``visibility='public'``, or kept for the owning group's future events when
    ``'private'``. Upserted per group by lower-cased name."""

    __tablename__ = "web_event_task_library"
    __table_args__ = (
        Index("uq_web_evt_library_name_source_group", "name", "source", "group_id", unique=True),
        Index("idx_web_evt_library_group", "group_id"),
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
    source = Column(String(24), nullable=False, default="legacy_v1")  # legacy_v1|group
    # Owning group for group-saved rows; NULL = site-wide (curated seeds, or
    # rows saved from global events by superadmins).
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    # EVENT_TASK_VISIBILITIES: public rows appear in every group's picker,
    # private rows only in the owning group's.
    visibility = Column(String(16), nullable=False, default="public", server_default="public")
    active = Column(Boolean, nullable=False, default=True)


# Version stamp written into EventTemplate.payload; bump when the snapshot
# shape changes and teach services/event_templates.py to upgrade old payloads.
EVENT_TEMPLATE_SCHEMA_VERSION = 1


class EventTemplate(Base):
    """A saved event *structure* — the "save/rerun events" feature.

    The whole-event analogue of ``EventTaskLibraryItem``: captured from an
    event in any lifecycle state, shared ``public`` (every clan's picker) or
    ``private`` (owning group only), and instantiated later as a fresh
    standard draft. The snapshot (config + tasks + bingo layout + team names,
    never runtime state) lives in ``payload`` as versioned JSON — templates
    are re-instantiated wholesale, so normalized child tables would only add
    migration drift. Queryable columns exist for the picker cards."""

    __tablename__ = "web_event_templates"
    __table_args__ = (
        Index("idx_web_evt_tmpl_group", "group_id", "active"),
        Index("idx_web_evt_tmpl_visibility", "visibility", "active"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    # Provenance only (SET NULL on event delete) — templates outlive events.
    source_event_id = Column(
        Integer, ForeignKey("web_events.id", ondelete="SET NULL"), nullable=True
    )
    # Owning group; NULL = site-wide (saved from a global event — always
    # treated as public regardless of visibility).
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    # EVENT_TASK_VISIBILITIES reused. Default private: publishing a whole
    # event site-wide is an explicit choice, unlike single library tasks.
    visibility = Column(String(16), nullable=False, default="private", server_default="private")
    # Source event's EVENT_MODES value — informational; instantiation always
    # produces a standard draft (clan bindings/invites are per-run).
    mode = Column(String(16), nullable=False, default="standard", server_default="standard")
    has_bingo = Column(Boolean, nullable=False, default=False)
    board_size = Column(Integer, nullable=False, default=5)
    task_count = Column(Integer, nullable=False, default=0)
    team_count = Column(Integer, nullable=False, default=0)
    schema_version = Column(Integer, nullable=False, default=EVENT_TEMPLATE_SCHEMA_VERSION)
    times_used = Column(Integer, nullable=False, default=0)
    payload = Column(Text, nullable=False)  # MEDIUMTEXT in MySQL (web36a)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
