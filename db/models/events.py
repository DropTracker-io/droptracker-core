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
    Float,
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

# How players get onto teams (web_events.formation_mode). All but admin_assign
# let players self-sign-up from the event page / a Discord button; they differ
# only in how a signed-up player reaches a team:
# - "self_join"    — the player picks their team at sign-up.
# - "auto_assign"  — the server places them immediately (balanced).
# - "signup_pool"  — sign-ups collect in a pool with NO team; admins sort them
#                    into teams later (manually or by repeated randomize).
# - "admin_assign" — no self sign-up at all; admins place every player.
EVENT_FORMATION_MODES = ("self_join", "auto_assign", "signup_pool", "admin_assign")

# Formation modes that let a player sign themselves up (everything but
# admin_assign). Used by the join route, the recruiting banner and the Discord
# signup button.
EVENT_SELF_SIGNUP_MODES = ("self_join", "auto_assign", "signup_pool")

# Task-library sharing (web_event_tasks.visibility / web_event_task_library.visibility):
# every task an admin creates is saved to the reusable task library — "public"
# rows show up in every group's picker, "private" rows only in the owning
# group's own picker (saved for their future events).
EVENT_TASK_VISIBILITIES = ("public", "private")

# Event ownership shape (web_events.mode): "standard" (one owning group, or
# global when group_id is NULL) vs "clan_vs_clan" (a host group plus invited
# opponent groups, tracked in web_event_groups).
EVENT_MODES = ("standard", "clan_vs_clan")

# Event game formats (web_events.kind) — ORTHOGONAL to ``mode`` (ownership):
# - "standard"   — a flat task list scored by completions.
# - "bingo"      — the task grid (has_bingo boards); put_bingo_board stamps it.
# - "board_game" — the dice-board mode (web43a+): tiles, turns, coins, shop.
# Which kinds a non-superadmin may CREATE is governed site-wide by the
# ``web_event_types`` registry (enabled/admin_only + per-type test-group
# allowlist) — see services/event_types.py. Existing events of a disabled
# kind keep running; the gate binds at creation only.
EVENT_KINDS = ("standard", "bingo", "board_game")

# web_event_groups participant roles / invite lifecycle.
EVENT_GROUP_ROLES = ("host", "opponent")
EVENT_GROUP_STATUSES = ("invited", "accepted", "declined")

# Ledger row lifecycle for web_event_completions.
EVENT_COMPLETION_STATUSES = ("auto", "pending", "confirmed", "rejected", "manual", "revoked")

# Per-event Discord destination kinds (web_event_channels.kind).
EVENT_CHANNEL_KINDS = ("announcements", "completions", "leaderboard", "admin")

EVENT_BOARD_SIZES = (3, 4, 5, 6, 7)  # square boards; default 5

# Task/tile difficulty tiers (legacy BoardGame elements, web44a): the tier a
# board tile draws its random task from, and the tier→coin ladder's key.
# Order matters — air is easiest, fire hardest.
EVENT_TASK_DIFFICULTIES = ("air", "water", "earth", "fire")

# Board-game tile roles (web_event_board_tiles.tile_kind).
EVENT_BOARD_TILE_KINDS = ("start", "normal", "special", "finish")

# Per-team turn state (web_event_board_positions.status):
# - "active"        — the team has a live task on its current tile.
# - "awaiting_roll" — task complete; waiting for the dice roll (manual
#                     trigger mode) before advancing.
# - "blocked"       — a roadblock/freeze effect is holding the team in place.
# - "finished"      — reached the finish tile.
EVENT_BOARD_POSITION_STATUSES = ("active", "awaiting_roll", "blocked", "finished")

# web_event_coin_ledger.reason values.
EVENT_COIN_REASONS = ("task_reward", "purchase", "refund", "admin", "bonus", "mercy")

# Shop item types (web_boardgame_shop_items.item_type) — the COOLDOWN
# grouping: a team may only use one item of a type every
# ``type_cooldown_turns`` turns (turn = completed-task count).
BOARDGAME_ITEM_TYPES = ("movement", "offensive", "defensive", "economy", "utility")

# Effect handler keys (web_boardgame_shop_items.effect). P2 ships the
# self-targeted set; the offensive/defensive handlers land in P3 (catalog
# rows may exist earlier but stay inactive).
BOARDGAME_EFFECTS = (
    "skip_task",        # complete the current task instantly (no coins)
    "reroll_task",      # redraw the current tile's task from its pool
    "boost_coins",      # multiply the next task's coin reward
    "advance",          # move forward N tiles without a task completion (P3)
    "roadblock",        # place a block on a tile (P3)
    "freeze_opponent",  # target team skips N turns (P3)
    "shield",           # negate the next offensive effect (P3)
)

# web_event_team_inventory.status lifecycle.
BOARDGAME_INVENTORY_STATUSES = ("owned", "used", "expired")

# web_event_effects.status lifecycle.
BOARDGAME_EFFECT_STATUSES = ("active", "consumed", "expired")

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

# How chatty the bot is about partial task progress (web_events.message_config
# JSON, key "task_progress"):
# - "off"        — only completions post (default; the pre-2026-07 behaviour).
# - "milestones" — a post when a team crosses 25/50/75% of a task's target.
# - "all"        — a post for every recorded increment (very verbose; the
#                  group leader's explicit choice).
EVENT_TASK_PROGRESS_MODES = ("off", "milestones", "all")

# notification_queue types a group leader can switch off per event via
# web_events.message_config (JSON {"toggles": {type: bool}, "task_progress":
# mode, "leaderboard": {live, top_n, show_tasks}}). Defaults and merge logic
# live in services/event_notifications.py (pure/stdlib so it stays
# unit-testable); admin-triggered types (event_signup_prompt) are
# deliberately absent — an explicit action always posts.
EVENT_MESSAGE_TOGGLE_KEYS = (
    "event_started",
    "event_ended",
    "event_completion",
    "event_task_progress",
    "event_line",
    "event_blackout",
    "event_lead_change",
    "event_pending",
    "event_activation_failed",
    # Board game (web44a): a team rolled + moved (+ the next task drawn).
    "event_board_turn",
)

# Message types that have a component-layout row in web_event_message_layouts:
# every queue type above plus the persistent live-standings board message.
EVENT_MESSAGE_LAYOUT_TYPES = EVENT_MESSAGE_TOGGLE_KEYS + (
    "event_signup_prompt",
    "event_board",
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
    # --- schema v2 (Task 15) ---
    formation_mode = Column(String(16), nullable=False, default="admin_assign")  # EVENT_FORMATION_MODES
    requires_confirmation = Column(Boolean, nullable=False, default=False)  # event-level force (PRD D3)
    submission_policy = Column(String(16), nullable=False, default="all")  # EVENT_SUBMISSION_POLICIES
    join_code = Column(String(32), nullable=True)  # optional self-join code; never in public reads
    discord_guild_id = Column(String(32), nullable=True)  # snowflake; any guild the bot is in (PRD D8)
    # EVENT_MODES. clan_vs_clan keeps group_id = the HOST group, so group
    # indexes, the default Discord guild, and entitlement gating work unchanged.
    mode = Column(String(16), nullable=False, default="standard", server_default="standard")
    # EVENT_KINDS — the game format (web43a). Orthogonal to ``mode``; the
    # web_event_types registry gates which kinds non-superadmins may create.
    kind = Column(String(24), nullable=False, default="standard", server_default="standard")
    # EVENT_DISCORD_POLICIES: when the Discord scheduled-event mirror goes
    # live — on activation (default; drafts create nothing on Discord) or
    # immediately at creation.
    discord_event_policy = Column(
        String(16), nullable=False, default="on_activate", server_default="on_activate"
    )
    # JSON {ping_key: [role snowflakes]} (EVENT_PING_KEYS) — which roles the
    # bot mentions on the scheduled-event companion message / lifecycle posts.
    ping_config = Column(Text, nullable=True)
    # JSON messaging verbosity knobs ({"toggles": {queue type: bool},
    # "task_progress": EVENT_TASK_PROGRESS_MODES, "leaderboard": {"live",
    # "top_n", "show_tasks"}}). NULL = all defaults; merge semantics in
    # services/event_notifications.py effective_message_config().
    message_config = Column(Text, nullable=True)
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
    # EVENT_TASK_DIFFICULTIES (web44a) — board-game tier. Rides over from the
    # library row when a task is added so difficulty-tiles have a filterable
    # roll pool; NULL on tasks that never set one (bingo/standard don't care).
    difficulty = Column(String(24), nullable=True)


class EventTeam(Base):
    __tablename__ = "web_event_teams"
    __table_args__ = (Index("idx_web_event_team_event", "event_id"), {"extend_existing": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    name = Column(String(80), nullable=False)
    score = Column(Integer, nullable=False, default=0)
    # The clan this team represents (clan_vs_clan only; NULL on standard/global).
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    # Admin-assigned accent color ("#rrggbb"); NULL = frontend palette default.
    color = Column(String(7), nullable=True)
    # Whole-clan fallback (clan_vs_clan): auto-created at activation when no
    # teams were set up. Credits every current member of ``group_id`` — no
    # explicit roster rows — so it runs as "anyone in this clan". See
    # services.event_lifecycle.activate_event / event_engine.load_matcher_state.
    auto_clan = Column(Boolean, nullable=False, default=False, server_default="0")
    # --- board game (web44a) ---
    # Coin wallet: running balance mirrored by the web_event_coin_ledger
    # audit trail; every write is += / -= under a row lock (the score pattern).
    coins = Column(Integer, nullable=False, default=0, server_default="0")
    # Game piece: an OSRS item id rendered via /img/itemdb/{id}.png (zero
    # upload), or a custom uploaded icon URL. item id wins when both set.
    piece_item_id = Column(Integer, nullable=True)
    piece_icon_url = Column(String(255), nullable=True)


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


class EventSignup(Base):
    """A player's self-service opt-in to an event ("Sign up!").

    One row per (event, player); the app layer additionally enforces one
    sign-up per *user* per event (a player enters with a single RSN). In
    ``signup_pool`` mode a sign-up carries NO team — admins later sort the pool
    into teams (creating ``web_event_team_members`` rows). In self_join /
    auto_assign the placement is immediate, but a sign-up row is still recorded
    so the pool view, Discord button and audit trail see a single source of
    truth for "who opted in". Events that never use self sign-up
    (admin_assign) write no rows here, so their behavior is unchanged."""

    __tablename__ = "web_event_signups"
    __table_args__ = (
        Index("uq_web_event_signup", "event_id", "player_id", unique=True),
        Index("idx_web_event_signup_event", "event_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    # The clan the player signs up under (clan_vs_clan); the event's group for a
    # standard group event; NULL for a global event.
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    # How the sign-up arrived, for the admin pool view: "web" | "discord".
    source = Column(String(16), nullable=False, default="web", server_default="web")
    created_at = Column(DateTime, default=func.now(), nullable=False)


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
    # The persistent bot message living in this channel, when the kind has one
    # (today: the 'leaderboard' kind's live standings board — the lootboard
    # pattern: post once, edit in place, repost if it vanishes). Written back
    # by the core bot (services/event_board.py); NULL until first post.
    message_id = Column(String(32), nullable=True)
    message_updated_at = Column(DateTime, nullable=True)


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


class EventMessageLayout(Base):
    """Component-layout template for one event Discord message type.

    The Components-V2 analogue of ``group_embeds`` (db/models/embed.py):
    one row per (group, message type), with the system defaults living on
    template group 1 and every group falling back to them unless it has the
    ``custom_embeds`` entitlement and its own row. ``layout`` is versioned
    JSON in the block DSL rendered by services/event_message_layouts.py
    ({"blocks": [{"type": "text"|"section"|"separator"|"standings"|"buttons",
    ...}]} with ``{placeholder}`` tokens) — kept as one JSON document rather
    than normalized child rows for the same reason as EventTemplate.payload:
    layouts are swapped wholesale, and the future web layout editor edits the
    whole tree at once."""

    __tablename__ = "web_event_message_layouts"
    __table_args__ = (
        Index("uq_web_event_msg_layout", "group_id", "message_type", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Group 1 = the template group whose rows are the seeded system defaults
    # (scripts/seed_event_message_layouts.py) — same convention as group_embeds.
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False, default=1)
    message_type = Column(String(32), nullable=False)  # EVENT_MESSAGE_LAYOUT_TYPES
    # Container accent bar, "#RRGGBB"; NULL = no accent.
    accent_color = Column(String(7), nullable=True)
    layout = Column(Text, nullable=False)  # MEDIUMTEXT in MySQL (web41a)
    schema_version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventType(Base):
    """Site-wide registry row for one event kind (web43a).

    The durable analogue of the seasonal Redis kill switch, but per game
    format and with an allowlist: superadmins toggle ``enabled`` /
    ``admin_only`` from /admin/event-types, and the CREATE gate
    (services/event_types.py::is_event_type_creatable) resolves to

        superadmin                    -> always allowed
        disabled OR admin_only        -> only groups in web_event_type_test_groups
        enabled + not admin_only      -> everyone (normal entitlement rules)

    The gate binds at creation only — existing events of a toggled-off kind
    keep running untouched. Seeded by the web43a migration; rows are never
    deleted (kinds are code-level concepts), only toggled."""

    __tablename__ = "web_event_types"
    __table_args__ = ({"extend_existing": True},)

    key = Column(String(24), primary_key=True)  # EVENT_KINDS
    label = Column(String(48), nullable=False)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    # Even while enabled, restrict creation to superadmins + test groups
    # (how a new kind ships dark: enabled for staff, invisible to everyone
    # else until the switch flips).
    admin_only = Column(Boolean, nullable=False, default=False, server_default="0")
    sort = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventTypeTestGroup(Base):
    """Per-kind test-group allowlist (web43a): admins of a listed group may
    create the kind even while it is disabled/admin_only — the "let this clan
    beta-test board games" mechanism. Managed from /admin/event-types."""

    __tablename__ = "web_event_type_test_groups"
    __table_args__ = (
        Index("uq_web_evt_type_test_group", "type_key", "group_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    type_key = Column(String(24), ForeignKey("web_event_types.key"), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    added_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


# --------------------------------------------------------------------------- #
# Board game (web44a) — the dice-board event kind.
# --------------------------------------------------------------------------- #
class EventBoardTile(Base):
    """One tile of a board-game event's track (the board designer's output).

    ``idx`` is the 0..N-1 sequence order along the track (advancement order —
    the LAST tile is the finish); ``x``/``y`` are fractional 0..1 positions on
    the background image so the overlay stays responsive at any render size.

    Task binding is difficulty-first: ``difficulty`` (the default mode) makes
    the tile ROLL a random task from the event's per-difficulty task pool on
    each landing, so different teams get different tasks; ``task_id`` pins one
    specific task instead. Both NULL = a rest tile (no task; roll again)."""

    __tablename__ = "web_event_board_tiles"
    __table_args__ = (
        Index("uq_web_board_tile_event_idx", "event_id", "idx", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    idx = Column(Integer, nullable=False)
    x = Column(Float, nullable=False, default=0.0)
    y = Column(Float, nullable=False, default=0.0)
    label = Column(String(255), nullable=True)
    difficulty = Column(String(24), nullable=True)  # EVENT_TASK_DIFFICULTIES
    task_id = Column(Integer, ForeignKey("web_event_tasks.id"), nullable=True)
    tile_kind = Column(
        String(16), nullable=False, default="normal", server_default="normal"
    )  # EVENT_BOARD_TILE_KINDS
    config = Column(Text, nullable=True)  # JSON: special-tile effects


class EventBoardConfig(Base):
    """Per-event board settings (one row per board-game event).

    ``settings`` is the full §2.5 config surface as one JSON document
    (movement/dice, tile_render, coins, shop, items, mercy, win) — swapped
    wholesale by the Board settings UI, defaulted key-by-key at read time by
    services/boardgame_engine.board_settings(), so a partial document never
    breaks a mechanic. The background image lives on the /img static tree or
    B2 depending on size; ``bg_width``/``bg_height`` preserve its aspect for
    the fractional tile overlay."""

    __tablename__ = "web_event_board_config"
    __table_args__ = (
        Index("uq_web_board_config_event", "event_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    background_url = Column(String(255), nullable=True)
    bg_width = Column(Integer, nullable=True)
    bg_height = Column(Integer, nullable=True)
    settings = Column(Text, nullable=True)  # JSON (§2.5); NULL = all defaults
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventBoardPosition(Base):
    """A team's live board state — the turn pointer the whole mode hangs off.

    One row per team, seeded at activation (tile 0, turn 0, first task).
    ``current_task_id`` is the ONLY task the engine evaluates submissions
    against for this team (board-game events filter the matcher to it);
    ``turns_completed`` is the turn counter item-type cooldowns compare
    against. ``mercy_deadline`` is the anti-stall auto-complete time (NULL
    when the mercy rule is off)."""

    __tablename__ = "web_event_board_positions"
    __table_args__ = (
        Index("idx_web_board_pos_event", "event_id"),
        {"extend_existing": True},
    )

    team_id = Column(Integer, ForeignKey("web_event_teams.id"), primary_key=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    tile_idx = Column(Integer, nullable=False, default=0)
    current_task_id = Column(Integer, ForeignKey("web_event_tasks.id"), nullable=True)
    turns_completed = Column(Integer, nullable=False, default=0)
    status = Column(
        String(16), nullable=False, default="active", server_default="active"
    )  # EVENT_BOARD_POSITION_STATUSES
    # Last roll, JSON {"dice": [3, 5], "from": 7, "to": 15, "at": unix} — lets
    # the client animate the exact faces and the audit trail reconstruct moves.
    last_roll = Column(Text, nullable=True)
    task_assigned_at = Column(DateTime, nullable=True)
    mercy_deadline = Column(DateTime, nullable=True)
    mercy_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class BoardgameShopItem(Base):
    """Site-wide curated shop catalog (web45a) — the power-ups teams buy with
    coins. Superadmin-managed (/admin/boardgame-shop); per-event availability
    and pricing live in ``EventShopRotation`` + the board settings' item kill
    switches. The ``PremiumFeature`` pattern: this is the product table."""

    __tablename__ = "web_boardgame_shop_items"
    __table_args__ = (
        Index("uq_web_bg_shop_key", "key", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(32), nullable=False)  # stable slug ("teleport_tablet")
    name = Column(String(80), nullable=False)
    description = Column(Text, nullable=True)
    # OSRS item id whose icon represents this power-up (/img/itemdb/{id}.png).
    icon_item_id = Column(Integer, nullable=True)
    item_type = Column(String(16), nullable=False)  # BOARDGAME_ITEM_TYPES
    effect = Column(String(24), nullable=False)     # BOARDGAME_EFFECTS
    effect_config = Column(Text, nullable=True)     # JSON: magnitude/duration/targets
    cost_coins = Column(Integer, nullable=False, default=0)
    # Turns before the team may use ANOTHER item of this item_type.
    type_cooldown_turns = Column(Integer, nullable=False, default=0)
    sort = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventShopRotation(Base):
    """Per-event shop stocking (web45a): which catalog items this event sells,
    at what price, in which turn window. No rows = the event sells every
    active catalog item at list price (minus the settings' kill switches) —
    zero-config events still get a shop."""

    __tablename__ = "web_event_shop_rotation"
    __table_args__ = (
        Index("uq_web_evt_shop_item", "event_id", "shop_item_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    shop_item_id = Column(Integer, ForeignKey("web_boardgame_shop_items.id"), nullable=False)
    price_override = Column(Integer, nullable=True)  # NULL = catalog price
    # Turn-window availability (P3 rotation); NULL bounds = always available.
    available_from_turn = Column(Integer, nullable=True)
    available_until_turn = Column(Integer, nullable=True)
    stock = Column(Integer, nullable=True)  # NULL = unlimited
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventTeamInventory(Base):
    """A team-held power-up (web45a) — the ``FeatureActivation`` analogue.
    One row per purchased copy; ``used_turn``/``used_on`` record consumption."""

    __tablename__ = "web_event_team_inventory"
    __table_args__ = (
        Index("idx_web_evt_inv_team", "event_id", "team_id", "status"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=False)
    shop_item_id = Column(Integer, ForeignKey("web_boardgame_shop_items.id"), nullable=False)
    price_paid = Column(Integer, nullable=False, default=0)
    acquired_turn = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="owned")  # BOARDGAME_INVENTORY_STATUSES
    used_turn = Column(Integer, nullable=True)
    used_at = Column(DateTime, nullable=True)
    used_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    used_on = Column(Text, nullable=True)  # JSON: {target_team_id?, target_tile_idx?}
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventTeamCooldown(Base):
    """Last turn a team used each item TYPE (web45a). The O(1) gate behind
    "each type usable once every N turns": usable iff
    ``turns_completed - last_used_turn >= type_cooldown_turns``."""

    __tablename__ = "web_event_team_cooldowns"
    __table_args__ = (
        Index("uq_web_evt_cooldown", "team_id", "item_type", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=False)
    item_type = Column(String(16), nullable=False)  # BOARDGAME_ITEM_TYPES
    last_used_turn = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventBoardEffect(Base):
    """A live board effect (web45a): coin boosts on self, and — P3 — road-
    blocks bound to tiles, freezes/shields bound to teams. Consumed or
    expired by the engine as turns advance."""

    __tablename__ = "web_event_effects"
    __table_args__ = (
        Index("idx_web_evt_effects_event", "event_id", "status"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    source_team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=False)
    target_team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=True)
    target_tile_idx = Column(Integer, nullable=True)
    effect_type = Column(String(24), nullable=False)  # BOARDGAME_EFFECTS
    effect_config = Column(Text, nullable=True)  # JSON: multiplier/turns/…
    expires_turn = Column(Integer, nullable=True)  # vs target team's turn counter
    status = Column(String(16), nullable=False, default="active")  # BOARDGAME_EFFECT_STATUSES
    inventory_id = Column(Integer, ForeignKey("web_event_team_inventory.id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventCoinLedger(Base):
    """Append-only audit trail behind ``EventTeam.coins`` (one row per grant/
    spend). The balance is the running column, NOT SUM(ledger) — the ledger
    exists so admins can answer "where did this team's coins go" and refunds
    can reference the original purchase row."""

    __tablename__ = "web_event_coin_ledger"
    __table_args__ = (
        Index("idx_web_coin_ledger_team", "event_id", "team_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=False)
    delta = Column(Integer, nullable=False)  # signed
    reason = Column(String(16), nullable=False)  # EVENT_COIN_REASONS
    # What this row credits/debits: e.g. ("task", task_id), ("purchase",
    # inventory row id), ("tile", tile idx).
    ref_type = Column(String(16), nullable=True)
    ref_id = Column(BigInteger, nullable=True)
    balance_after = Column(Integer, nullable=False)
    acted_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
