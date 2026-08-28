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
    # Pet acquisition — a specific pet by name, "any pet" / a pet category
    # (boss/skilling/raids/…) resolved via utils.osrs_pets, or an explicit
    # config.pets allow list (customized category preset). See event_engine.
    "pet_collection",
    # Loot Sweep (loot_sweep kind): one task per boss/"set". Each config item
    # awards points that DECAY per successive team receipt, capped per item;
    # collecting a full set awards a bonus (capped). Scored continuously off
    # the ledger — never "completes". Config/scoring: services/loot_sweep.py.
    "loot_sweep",
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

# Event-level audience (web_events.visibility): "public" — anyone can see the
# event on the site/Activity/lists (the default, unchanged behaviour);
# "private" — only members of the participating group(s) and event admins ever
# see it, at ANY lifecycle status (the same audience a pre-publication draft is
# limited to, but permanent). Distinct from EVENT_TASK_VISIBILITIES, which is
# only about task-library reuse.
EVENT_VISIBILITIES = ("public", "private")

# Event ownership shape (web_events.mode): "standard" (one owning group, or
# global when group_id is NULL) vs "clan_vs_clan" (a host group plus invited
# opponent groups, tracked in web_event_groups).
EVENT_MODES = ("standard", "clan_vs_clan")

# Event game formats (web_events.kind) — ORTHOGONAL to ``mode`` (ownership):
# - "standard"   — a flat task list scored by completions.
# - "bingo"      — the task grid (has_bingo boards); put_bingo_board stamps it.
# - "board_game" — the dice-board mode (web43a+): tiles, turns, coins, shop.
# - "loot_sweep" — obtain items across the game for points that decay per
#                  successive team receipt (capped per item), plus boss-"set"
#                  completion bonuses. One EventTask (type 'loot_sweep') per
#                  set; scoring is continuous off the ledger. See
#                  services/loot_sweep.py + docs/LOOT_SWEEP.md.
# Which kinds a non-superadmin may CREATE is governed site-wide by the
# ``web_event_types`` registry (enabled/admin_only + per-type test-group
# allowlist) — see services/event_types.py. Existing events of a disabled
# kind keep running; the gate binds at creation only.
EVENT_KINDS = ("standard", "bingo", "board_game", "loot_sweep")

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
# - "blocked"       — a tile effect (roadblock stall) is holding the team in
#                     place: roll attempts are consumed without movement until
#                     turns_completed reaches blocked_until_turn.
# - "finished"      — reached the finish tile.
EVENT_BOARD_POSITION_STATUSES = ("active", "awaiting_roll", "blocked", "finished")

# web_event_coin_ledger.reason values.
# "toll" (web50a): a coin_toll transfer between teams (both the victim debit and
# the mover credit rows use it).
EVENT_COIN_REASONS = ("task_reward", "purchase", "refund", "admin", "bonus", "mercy", "toll")

# Shop item types (web_boardgame_shop_items.item_type) — the COOLDOWN
# grouping: a team may only use one item of a type every
# ``type_cooldown_turns`` turns (turn = completed-task count).
BOARDGAME_ITEM_TYPES = ("movement", "offensive", "defensive", "economy", "utility")

# Effect handler keys (web_boardgame_shop_items.effect). Handlers live in
# services/boardgame_shop.py (_use_<key>); per-effect metadata + behavior
# defaults (tile-bound? break_on/stall_turns/visible_to_all) live in
# services/boardgame_effects.py (EFFECT_REGISTRY).
BOARDGAME_EFFECTS = (
    "skip_task",        # complete the current task instantly (no coins)
    "reroll_task",      # redraw the current tile's task from its pool
    "boost_coins",      # multiply the next task's coin reward
    "advance",          # move forward N tiles without a task completion (P3)
    "roadblock",        # place a block on a tile (P3)
    "freeze_opponent",  # target team skips N turns (P3)
    "shield",           # negate the next offensive effect (P3)
    # -- web50a shop expansion --------------------------------------------
    "extra_dice",           # next roll adds N dice (movement)
    "choose_roll",          # next roll is forced to a chosen value (movement)
    "reroll_move",          # re-roll the team's previous move (movement)
    "ward",                 # negate specific offensive effect keys (defensive)
    "cleanse",              # clear the team's active negative effects (defensive)
    "choose_task",          # pick from N candidate tasks for the tile (utility)
    "steal_item",           # steal a random owned item from a rival (offensive)
    "reroll_opponent_task",  # reroll a rival team's current task (offensive)
    "knockback",            # push a rival team back N tiles (offensive)
    "coin_toll",            # next roll tolls passed-over rival teams (economy)
)

# web_event_team_inventory.status lifecycle.
#   owned    — bought, unused, in the bag
#   used     — consumed by its effect
#   expired  — no longer valid (reserved)
#   refunded — auto-refunded because the item became unusable (its effect was
#              disabled mid-event, or has no live handler); coins credited back
BOARDGAME_INVENTORY_STATUSES = ("owned", "used", "expired", "refunded")

# web_event_effects.status lifecycle.
BOARDGAME_EFFECT_STATUSES = ("active", "consumed", "expired")

# Which intake paths may drive automatic task progress (web_events.submission_policy):
# - "all"             — every processed submission counts (API + webhook fallback).
# - "confirm_non_api" — non-plugin (manual) submissions count but always land
#                       as pending ledger rows needing admin confirmation.
#                       DEFAULT for new events (2026-07-17).
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
EVENT_PING_KEYS = ("event_created", "event_started", "event_ended",
                   # web82a: a recurring-schedule scoring window opened —
                   # "the weekend is live, get on" is exactly what role pings
                   # exist for. Keyed by notification type (the sender maps
                   # ping_config[notification_type] directly).
                   "event_window_opened")

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
    # Recurring schedules (web82a): a scoring window opened / closed on an
    # event that runs in repeating windows (e.g. weekends only). The event
    # stays live between windows, so these are distinct from started/ended.
    "event_window_opened",
    "event_window_closed",
    "event_completion",
    "event_task_progress",
    "event_line",
    "event_blackout",
    "event_lead_change",
    "event_pending",
    "event_activation_failed",
    # P0-8: scheduled end failed / wrap-up incomplete — an admin must look.
    "event_end_failed",
    # Board game (web44a): a team rolled + moved (+ the next task drawn).
    "event_board_turn",
    # Board game (web53a): "task done — roll the dice" nudge. Default OFF for
    # the event's main channels (team channels carry it by default instead —
    # services/event_team_discord.DEFAULT_TEAM_MESSAGE_TOGGLES).
    "event_board_roll_prompt",
    # Board game (web61a): a team used an offensive/defensive item on a rival
    # (froze / knocked back / stole / rerolled), or a defense blocked one — so
    # the PvP layer is visible instead of silently mutating another team's board.
    "event_board_action",
    # Loot Sweep (loot_sweep kind): three independently-toggleable verbosity
    # levels. Individual item receipts default OFF (a 300-item game-wide sweep
    # would flood the channel); subset + whole-set completions default ON.
    "event_sweep_item",
    "event_sweep_group",
    "event_sweep_set",
)

# Message types that have a component-layout row in web_event_message_layouts:
# every queue type above plus the persistent live-standings board message.
EVENT_MESSAGE_LAYOUT_TYPES = EVENT_MESSAGE_TOGGLE_KEYS + (
    "event_signup_prompt",
    # web70a — layout-only key (like event_board_win): what a posted sign-up
    # prompt is re-rendered into once sign-ups close, with the button gone.
    # Never queued; edited over the original prompt message in place.
    "event_signup_closed",
    "event_board",
    # Prize pot (web52a) — the manual "advertise the pot now" post. Admin
    # action, no verbosity toggle (absent from EVENT_MESSAGE_TOGGLE_KEYS).
    "event_pot",
    # Board-game victory (audit): layout-only key — queued as
    # event_board_turn with data.won, remapped at render time so a winning
    # roll doesn't read as a mundane dice move. Toggled via event_board_turn.
    "event_board_win",
)

# Team-leadership roles a roster row may carry (web_event_team_members.role;
# NULL = plain member). Leaders hold executive authority for their team —
# today that gates board-game turn actions (roll/shop); co-leaders share it.
EVENT_TEAM_ROLES = ("leader", "co_leader")

# How a team's leader is chosen (web_events.leadership_config JSON key
# "selection"):
# - "admin"    — event admins assign/remove leaders (default).
# - "election" — team members vote for a teammate; a strict plurality wins.
#                Admin assignment still works as an override either way.
EVENT_LEADER_SELECTION_MODES = ("admin", "election")

# Prize-pot ledger (web52a): per-participant GP buy-ins + donations, summed into
# an advertised pot. The tool tracks/advertises GP only — payouts are traded
# in-game by the clan (like split-tracking); nothing here moves real GP or
# EventTeam.score.
# web_event_buyins.kind — a stake to enter vs. an extra/standalone gift (a
# donation may come from a non-participant, e.g. a leader sweetening the pot).
EVENT_BUYIN_KINDS = ("buyin", "donation")
# web_event_buyins.status — only "paid" rows count toward the pot. The admin
# "tick" flips pledged->paid; donations default paid; "void" = soft-removed
# (kept for audit, so re-enabling restores the pot).
EVENT_BUYIN_STATUSES = ("pledged", "paid", "void")
# web_events.prize_config "distribution" — who is *advertised* as taking the
# pot (advisory display + optional Discord line, NOT an automated transfer):
# first place only, the top N teams, or a custom percentage split by place.
EVENT_PRIZE_DISTRIBUTIONS = ("first_only", "top_n", "custom_split")

# Per-team Discord provisioning (web53a) — what happens to the auto-created
# team roles/channels when the event ends NATURALLY: torn down after a ~48h
# grace window (pings stay usable for post-event wrap-up), or kept forever.
# A hard event delete always tears down immediately (orphan queue), and this
# never applies to drafts. See services/event_team_discord.py.
EVENT_TEAM_DISCORD_RETENTIONS = ("delete_48h", "keep")

# web_event_team_discord.channel_kind — a normal guild text channel vs. a
# thread auto-created inside the configured forum channel.
EVENT_TEAM_CHANNEL_KINDS = ("text", "thread")


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
    # EVENT_VISIBILITIES. "private" hides the event from the public site/lists —
    # only participating-group members + event admins ever see it, at any
    # status (same audience as a pre-publication draft, but permanent).
    visibility = Column(
        String(16), nullable=False, default="public", server_default="public"
    )
    starts_at = Column(DateTime, nullable=True)
    ends_at = Column(DateTime, nullable=True)
    has_bingo = Column(Boolean, nullable=False, default=False)
    # --- schema v2 (Task 15) ---
    formation_mode = Column(String(16), nullable=False, default="admin_assign")  # EVENT_FORMATION_MODES
    requires_confirmation = Column(Boolean, nullable=False, default=False)  # event-level force (PRD D3)
    submission_policy = Column(String(16), nullable=False, default="confirm_non_api")  # EVENT_SUBMISSION_POLICIES
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
    # JSON team-leadership knobs ({"enabled": bool, "co_leaders": bool,
    # "selection": EVENT_LEADER_SELECTION_MODES}). NULL = disabled. Merge
    # semantics in services/event_leadership.effective_leadership().
    leadership_config = Column(Text, nullable=True)
    # Clan-vs-clan only: each participating clan configures its OWN Discord
    # channels (web_event_channels.group_id) and verbosity
    # (web_event_groups.message_config) — notifications fan out to every
    # accepted clan's destinations instead of the single host-configured set.
    per_group_discord = Column(Boolean, nullable=False, default=False, server_default="0")
    board_size = Column(Integer, nullable=False, default=5)  # EVENT_BOARD_SIZES
    bonus_line_points = Column(Integer, nullable=False, default=0)
    bonus_blackout_points = Column(Integer, nullable=False, default=0)
    # Prize pot (web52a): master toggle + JSON knobs. buyins_enabled is a cheap
    # scalar every "show the pot?" check reads (and gates the confirm-on-disable
    # guard); prize_config merges through web_api.event_prizes
    # .effective_prize_config() (default_buyin, distribution, advertise,
    # show_contributors, allow_leader_mark). NULL = all defaults. See EventBuyin.
    buyins_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    prize_config = Column(Text, nullable=True)
    # Per-team Discord provisioning (web53a): JSON knobs merged through
    # services.event_team_discord.effective_team_discord_config()
    # ({"channels_enabled", "roles_enabled", "forum_channel_id", "retention",
    # "captain_config", "teams": {team_id: {"role", "channel", "toggles"}}}).
    # NULL = feature off. Desired state materializes into
    # web_event_team_discord rows; only the core bot touches Discord.
    team_discord_config = Column(Text, nullable=True)
    # Live edits (web68a): opt-in toggle letting event admins keep editing the
    # bingo board after activation (the one structural surface locked once an
    # event starts). Settable in the wizard and flippable at any status via
    # PATCH /events; every flip lands in the event.settings.update audit diff.
    allow_live_edits = Column(Boolean, nullable=False, default=False, server_default="0")
    # EHE visibility (web74a): "public" shows each player's Efficient Hours
    # towards Event on the team/player surfaces; "admins" keeps the figure to
    # the event managers' effort report. Some clans don't want a per-member
    # effort number on a public page — it invites "you did the least" — so the
    # data is always RECORDED and only the display is gated. Public is the
    # default so existing events keep behaving as they do today.
    effort_visibility = Column(String(16), nullable=False,
                               default="public", server_default="public")
    # Late sign-ups (web70a): OFF (default) closes self sign-ups the moment the
    # event begins — the Discord prompt retires its button, the join panel and
    # the API refuse. ON restores the pre-web70a behaviour: players may still
    # enter mid-event, right up to the end. Admins can always place players
    # manually either way (the roster stays open until the event is past).
    allow_late_signups = Column(Boolean, nullable=False, default=False, server_default="0")
    # Recurring activation schedule (web82a): JSON rule describing WHEN inside
    # [starts_at, ends_at] scoring is open (e.g. every weekend of the month,
    # all weekends counting as one event). NULL = continuous (every event
    # before web82a). The rule is validated + compiled by
    # services/event_schedule.py into explicit web_event_windows rows — every
    # consumer reads THOSE, never this JSON. Between windows the event stays
    # 'active' (channels/boards/pages up) but submissions credit nothing.
    # Not available for kind='board_game' (its turn/shop clocks are wall-clock
    # and would keep ticking while scoring is closed).
    schedule_config = Column(Text, nullable=True)
    activated_at = Column(DateTime, nullable=True)
    ended_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class EventWindow(Base):
    """One materialized ``[starts_at, ends_at)`` scoring window of a
    recurring-schedule event (web82a). Compiled from
    ``Event.schedule_config`` by services/event_schedule.py whenever the
    schedule or the event dates change; ``seq`` is the 0-based chronological
    position. ``source`` records provenance: 'rule' (compiled) or 'frozen'
    (a fully-elapsed window preserved verbatim across a mid-event schedule
    edit, so late-arriving submissions from it keep their credit). Events
    without a schedule have no rows here."""

    __tablename__ = "web_event_windows"
    __table_args__ = (
        Index("idx_web_event_window_event", "event_id", "starts_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    seq = Column(Integer, nullable=False, default=0)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    source = Column(String(16), nullable=False, default="rule",
                    server_default="rule")
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventTask(Base):
    __tablename__ = "web_event_tasks"
    __table_args__ = (Index("idx_web_event_task_event", "event_id"), {"extend_existing": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    type = Column(String(24), nullable=False)
    label = Column(String(255), nullable=False)
    target = Column(String(120), nullable=True)
    # BigInteger (web69a): loot_value goals are raw GP, and a multi-billion GP
    # target blew past signed-INT 2.147B — the INSERT died with MySQL 1264
    # rather than a readable error (same lesson as ``EventCompletion.quantity``,
    # P1-7, which this is compared against).
    target_value = Column(BigInteger, nullable=True)
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
    # DOUBLE (2026-07-19): loot_sweep awards decimal points (a 1-pointer's
    # second receipt at 20% decay is 0.8). Every write rounds to 2dp; other
    # event kinds keep integral values.
    score = Column(Float(53), nullable=False, default=0)
    # The clan this team represents (clan_vs_clan only; NULL on standard/global).
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    # Admin-assigned accent color ("#rrggbb"); NULL = frontend palette default.
    color = Column(String(7), nullable=True)
    # Short label for surfaces with no room for the full name — today the
    # in-game clan-chat badge (web103a). NULL means "derive one from the name"
    # (services.event_teams.derive_short_tag), so a team that never sets one
    # still gets a stable tag.
    short_tag = Column(String(8), nullable=True)
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
    # Per-clan messaging verbosity for Event.per_group_discord: same JSON shape
    # as web_events.message_config (stored fully merged, like the event's).
    # NULL = inherit the event-level config.
    message_config = Column(Text, nullable=True)
    # The guild this clan's per-group channels live in (channel pickers +
    # validation; sending only needs the channel snowflakes). NULL until the
    # clan configures per-group Discord.
    discord_guild_id = Column(String(32), nullable=True)
    # Per-team Discord provisioning for THIS clan's guild (web53a, clan-vs-clan
    # only): same JSON shape as web_events.team_discord_config, scoped to this
    # clan's own teams + its discord_guild_id. NULL = this clan hasn't enabled
    # it (there is deliberately no inheritance from the event-level config —
    # nothing gets created in a clan's server unasked).
    team_discord_config = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventTeamMember(Base):
    __tablename__ = "web_event_team_members"
    __table_args__ = (
        # web59a (P0-9): "one team per event" as a DB constraint — the
        # check-then-insert join flow races, and two concurrent joins can put
        # a player on two teams (double-crediting every drop). The composite
        # PK can't catch that (different team_id); the denormalized event_id
        # can. Every creation site must set event_id.
        Index("uq_web_evt_member_event_player", "event_id", "player_id",
              unique=True),
        {"extend_existing": True},
    )

    team_id = Column(Integer, ForeignKey("web_event_teams.id"), primary_key=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    # Credit cutoff: the engine ignores submissions timestamped earlier (PRD D10).
    joined_at = Column(DateTime, default=func.now(), nullable=False)
    # EVENT_TEAM_ROLES ("leader"/"co_leader"); NULL = plain member. Only
    # meaningful when the event's leadership_config enables leadership.
    role = Column(String(16), nullable=True)


class EventLeaderVote(Base):
    """One team member's vote for who should lead their team (election mode).

    One live vote per (event, voter) — re-voting replaces it. The tally runs
    after every vote: a candidate with a STRICT plurality of the team's votes
    becomes leader (services/event_leadership.py); ties leave the current
    leader in place. Admin assignment always overrides."""

    __tablename__ = "web_event_leader_votes"
    __table_args__ = (
        Index("uq_web_evt_leader_vote", "event_id", "voter_player_id", unique=True),
        Index("idx_web_evt_leader_vote_team", "event_id", "team_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=False)
    voter_player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    candidate_player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


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


class EventSignupMessage(Base):
    """A sign-up prompt the bot posted to Discord (web70a).

    The "Sign up" post is an ordinary queued notification, so nothing used to
    remember where it landed — and a prompt whose sign-ups had closed kept
    showing its button forever. One row per (event, message) is written by
    services/notification_service.py as each destination send succeeds; the
    core bot's retire sweep (services/event_signup_prompt.py) edits those
    messages into the closed layout, drops the button and stamps
    ``closed_at``. Same shape as the leaderboard board post: post once, edit
    in place, give up quietly if the message is gone.
    """

    __tablename__ = "web_event_signup_messages"
    __table_args__ = (
        Index("uq_web_evt_signup_msg", "event_id", "message_id", unique=True),
        Index("idx_web_evt_signup_msg_open", "closed_at", "event_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    channel_id = Column(String(32), nullable=False)
    message_id = Column(String(32), nullable=False)
    # Which clan's channel this landed in (per-group clan-vs-clan fan-out);
    # NULL for the event's shared/host destination.
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    posted_at = Column(DateTime, default=func.now(), nullable=False)
    # When the prompt was retired (button removed). NULL = still live; the
    # sweep only looks at NULL rows. Also stamped when the message turns out
    # to be deleted, so a vanished post stops being retried.
    closed_at = Column(DateTime, nullable=True)


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
    # BigInteger (P1-7): loot_value tasks fold raw GP here — a large multi-day
    # event can exceed signed-INT 2.147B and poison every later matching drop.
    quantity = Column(BigInteger, nullable=False, default=1)
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


class EventBuyin(Base):
    """Prize-pot ledger (web52a): one row per participant buy-in or donation.

    A **buy-in** is a participant's stake to enter (``kind='buyin'``) with an
    expected ``amount`` and a paid tick (``status`` pledged->paid); a
    **donation** is extra/standalone GP (``kind='donation'``, defaults
    ``paid``, may come from a non-participant). The advertised prize pot is
    ``SUM(amount) WHERE status='paid'``.

    A deliberate sibling of :class:`EventCompletion`, NOT part of it: pot GP
    must never move ``EventTeam.score`` (the competitive source of truth), so
    it stays entirely off the ``apply_completion`` fold path. ``amount`` is
    BigInteger — a large pot exceeds signed-INT 2.147B (the same lesson as
    ``EventCompletion.quantity``, P1-7)."""

    __tablename__ = "web_event_buyins"
    __table_args__ = (
        Index("idx_web_evt_buyin_event", "event_id", "status"),
        Index("idx_web_evt_buyin_team", "event_id", "team_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    # The team this contribution credits; NULL = pot-wide / unassigned donation.
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=True)
    # The RSN who paid/donated; NULL for a free-text external donor (a non-roster
    # sponsor topping up the pot).
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=True)
    # Display snapshot, or the free-text donor name when there is no player_id.
    rsn = Column(String(24), nullable=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)  # discord user, if resolvable
    kind = Column(String(16), nullable=False, default="buyin")  # EVENT_BUYIN_KINDS
    amount = Column(BigInteger, nullable=False, default=0)  # GP — BigInteger (see docstring)
    status = Column(String(16), nullable=False, default="pledged")  # EVENT_BUYIN_STATUSES
    note = Column(String(255), nullable=True)
    # Optional screenshot backing this contribution — the trade window, the
    # "you have received" chat line (web75a). Same shape and role as
    # :attr:`EventCompletion.proof_url`: a public CDN URL derived server-side
    # from an uploaded object key, never a client-supplied address.
    proof_url = Column(String(255), nullable=True)
    # Admin/leader who last recorded or ticked this row.
    acted_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    paid_at = Column(DateTime, nullable=True)  # stamped when status -> paid


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
    # DOUBLE (2026-07-19, was BigInteger): loot_value progress folds raw GP
    # (integers stay exact to 2^53 ≈ 9e15) and loot_sweep running totals are
    # 2-decimal point values.
    progress = Column(Float(53), nullable=False, default=0)
    completed = Column(Boolean, nullable=False, default=False)
    completed_at = Column(DateTime, nullable=True)


class EventPlayerPoints(Base):
    """Per-player contribution points, one row per (task, team, player).

    Written when a task completes: the task's points are split across the
    applied-ledger contributors by their net quantity share (a 5-point task
    done 50/50 awards 2.5 each — hence FLOAT). The integer team score stays
    the competitive source of truth; this is the stat that lets end-of-event
    player points correlate with actual contribution. Rows are rewritten
    (or deleted) when a revoke changes the task's completion state — see
    services.event_engine._award_contribution_points."""

    __tablename__ = "web_event_player_points"
    __table_args__ = (
        Index("uq_web_evt_player_points", "task_id", "team_id", "player_id", unique=True),
        Index("idx_web_evt_player_points_event", "event_id", "player_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("web_event_tasks.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    points = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventEffort(Base):
    """Bingo EHB — kills a player put into an event's relevant NPCs, one row
    per (event, player, npc), whether or not anything dropped.

    Its own table rather than a column anywhere existing, for two reasons:
    ``EventProgress`` is per (task, team) with no ``player_id`` at all, and
    ``EventCompletion`` is the *credit* ledger — effort must never inflate
    contribution counts, points, or the "Last …" line.

    Not per task: an NPC feeding three tiles is still one in-game kill counter.
    ``frozen_at`` is stamped when every task the NPC feeds has completed for
    the team, after which the row stops accruing (kills already banked stay).
    See ``services/event_effort.py`` for the scoring rules.
    """

    __tablename__ = "web_event_effort"
    __table_args__ = (
        Index("uq_web_evt_effort", "event_id", "player_id", "npc_id", unique=True),
        Index("idx_web_evt_effort_team", "event_id", "team_id"),
        # The inactivity report sorts a whole event's roster by recency.
        Index("idx_web_evt_effort_last", "event_id", "last_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    # Stamped from the roster at write time so the admin report can group by
    # team without a join; a player belongs to exactly one team per event.
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    npc_id = Column(Integer, ForeignKey("npc_list.npc_id"), nullable=False)
    # WOM boss slug, denormalized so the read path can price EHB without
    # re-resolving names. NULL = tracked activity with no WOM rate (0 EHB).
    boss_metric = Column(String(48), nullable=True)
    kills = Column(BigInteger, nullable=False, default=0)
    # Attempts that reached the WOM-counted event, at the NPCs where those are
    # not the same thing — see services/event_effort.COMPLETION_MARKERS. Always
    # 0 elsewhere, where every kill is a completion by definition. May exceed
    # `kills`: WOM reports completions for a player whose plugin never sent an
    # attempt, so the attempt total is max() of the two, not `kills` alone.
    completions = Column(BigInteger, nullable=False, default=0,
                         server_default="0")
    # 'plugin', 'wom', or 'both' — which side of the hybrid fold fed this row.
    # Freeze precision depends on it: plugin folds are real-time and freeze
    # exactly, WOM snapshots lag and lose the tail (documented approximation).
    source = Column(String(16), nullable=False, default="plugin")
    first_at = Column(DateTime, default=func.now(), nullable=False)
    last_at = Column(DateTime, default=func.now(), nullable=False)
    frozen_at = Column(DateTime, nullable=True)


class EventChannel(Base):
    """Per-event Discord destination (PRD D8)."""

    __tablename__ = "web_event_channels"
    __table_args__ = (
        # Renamed from uq_web_event_channel (web48a): the old (event_id, kind)
        # index backs the event_id FK, so the widened index must be CREATED
        # under a new name before the old one can drop.
        Index("uq_web_event_channel_grp", "event_id", "kind", "group_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    kind = Column(String(24), nullable=False)  # EVENT_CHANNEL_KINDS
    channel_id = Column(String(32), nullable=False)
    # Owning clan for per-group clan-vs-clan configs (Event.per_group_discord):
    # NULL = the event's shared/host config (the only shape before web48a).
    # App-layer upserts key on the exact (event_id, kind, group_id) triple —
    # MySQL unique indexes treat NULLs as distinct, so the index alone can't.
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
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


class EventTeamDiscord(Base):
    """Per-(team, guild) Discord provisioning state (web53a).

    The desired-state sibling of :class:`EventGuild`, for auto-created team
    roles and team channels/threads. The Web API only ever writes *desired*
    rows here (``services/event_team_discord.py``: config PUT, activation,
    roster/team mutations, end-of-life retirement) and never talks to
    Discord; the core bot's ``reconcile_event_team_discord`` task is the only
    place that creates/renames/deletes the real role + channel and writes
    back ``role_id``/``channel_id``. Idempotent: a row that already carries
    an id is edited, never re-created.

    One row per guild keeps clan-vs-clan sane: role/channel ids are
    guild-specific, and each participating clan provisions only its own teams
    into its own server. ``member_state`` is the JSON list of Discord user
    ids the bot last applied — roster mutations just flip ``members_dirty``
    and the bot diffs against it (no per-change Discord calls from the API).
    """

    __tablename__ = "web_event_team_discord"
    __table_args__ = (
        Index("uq_web_evt_team_discord", "team_id", "guild_id", unique=True),
        Index("idx_web_evt_team_discord_event", "event_id", "sync_status"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id"), nullable=False)
    team_id = Column(Integer, ForeignKey("web_event_teams.id"), nullable=False)
    guild_id = Column(String(32), nullable=False)  # snowflake
    # Which config scope provisioned this row: NULL = the event-level config
    # (web_events.team_discord_config); a group id = that clan's own config
    # (web_event_groups.team_discord_config, clan-vs-clan).
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=True)
    # Written back by the bot. channel_id is a text channel or a forum thread
    # depending on channel_kind (EVENT_TEAM_CHANNEL_KINDS).
    role_id = Column(String(32), nullable=True)
    channel_id = Column(String(32), nullable=True)
    channel_kind = Column(String(16), nullable=True)
    # Optional temporary team VOICE channel (voice_enabled) — same access
    # gating and teardown rules as the text channel.
    voice_channel_id = Column(String(32), nullable=True)
    sync_status = Column(String(16), nullable=False, default="pending")  # pending|synced|delete_pending|failed
    # JSON list of Discord user ids currently carrying the role / thread
    # membership; the bot's diff baseline for roster sync.
    member_state = Column(Text, nullable=True)
    members_dirty = Column(Boolean, nullable=False, default=True, server_default="1")
    # The team channel's primary board post (web54a): the bot's own message
    # holding the team-filtered live board image + quick links, posted once and
    # edited in place (the lootboard / event_board pattern). board_state_hash
    # is the last rendered state signature — the refresher skips rows whose
    # board hasn't visually changed.
    board_message_id = Column(String(32), nullable=True)
    board_state_hash = Column(String(64), nullable=True)
    board_updated_at = Column(DateTime, nullable=True)
    # The team channel's lootboard post (web93a): a SECOND bot-owned message
    # carrying the team's generated event lootboard PNG
    # (``lootboard/team_boards.py``), posted directly beneath the board post
    # above and edited in place. ``loot_state_hash`` is the delivered image's
    # content signature, so a tick whose PNG has not changed costs no Discord
    # call at all. Deliberately never pinned — the board post is the pinned
    # one — and only ever written while ``EVENT_TEAM_LOOTBOARDS`` is on.
    loot_message_id = Column(String(32), nullable=True)
    loot_state_hash = Column(String(64), nullable=True)
    loot_updated_at = Column(DateTime, nullable=True)
    # Natural-end grace deadline (retention 'delete_48h'): the bot tears the
    # role/channel down once now > delete_after. NULL = no scheduled teardown
    # (either still live, or an immediate delete_pending from a team removal).
    delete_after = Column(DateTime, nullable=True)
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
    target_value = Column(BigInteger, nullable=True)  # BigInteger — see EventTask.target_value
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
        # _v2: the widened (…, event_id) unique replaced the web41a two-column
        # index under a new name — the group_id FK needs a supporting index at
        # every moment of the migration, so it was create-new-then-drop-old.
        Index("uq_web_event_msg_layout_v2", "group_id", "message_type", "event_id",
              unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Group 1 = the template group whose rows are the seeded system defaults
    # (scripts/seed_event_message_layouts.py) — same convention as group_embeds.
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False, default=1)
    # web66a: 0 = a group-level layout; a real event id = a one-event override
    # (stored under the event's host group, template group 1 for global events)
    # resolved ahead of the group default. A 0 sentinel, not NULL — MySQL
    # unique indexes admit unlimited NULL duplicates. No FK (0 references no
    # row); rows are cleaned up with the event's delete cascade.
    event_id = Column(Integer, nullable=False, default=0, server_default="0")
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


# Sentinel type_key on EventRateLimit rows: the rule caps a group's TOTAL
# events (every kind combined) rather than one kind. A real value (not NULL)
# so the (tier_key, type_key) unique index actually binds — MySQL unique
# indexes allow unlimited NULL duplicates.
EVENT_RATE_LIMIT_ALL_TYPES = "*"


class EventRateLimit(Base):
    """Site-wide per-tier event frequency cap (web65a).

    One row = "groups on subscription tier ``tier_key`` may run at most
    ``max_events`` events of ``type_key`` per rolling ``window_days`` days".
    ``type_key`` is an EVENT_KINDS value, or ``"*"``
    (EVENT_RATE_LIMIT_ALL_TYPES) for the tier's TOTAL cap across every kind.

    Semantics (db/event_rate_limits.py is the read/enforcement side):
    - No row for a (tier, kind) → that scope is unlimited; the tier's normal
      ``events`` entitlement + ``events_max_active`` rules stand alone.
    - Rows bind at ACTIVATION (drafts stay unlimited, matching
      events_max_active): an event counts against the window the moment it
      goes live, via ``web_events.activated_at``.
    - Any enabled row with ``max_events`` > 0 also GRANTS rate-limited event
      access to tiers whose ``events`` entitlement is off — how the free tier
      gets "an event every so often" without unlocking the full firehose.
      With no rows configured (the launch baseline) nothing changes: events
      stay patron-only.

    Superadmins configure rows on /admin/event-limits; nothing seeds them.
    """

    __tablename__ = "web_event_rate_limits"
    __table_args__ = (
        Index("uq_web_event_rate_limit", "tier_key", "type_key", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tier_key = Column(String(40), ForeignKey("subscription_tiers.key"), nullable=False)
    # EVENT_KINDS value or "*" — deliberately NOT an FK to web_event_types
    # (the sentinel isn't a registry row).
    type_key = Column(String(24), nullable=False, default=EVENT_RATE_LIMIT_ALL_TYPES)
    # 0 is meaningful: "none of this kind at all" (an explicit block, distinct
    # from "no row" = unlimited).
    max_events = Column(Integer, nullable=False)
    window_days = Column(Integer, nullable=False)
    # Off = keep the numbers but stop enforcing/granting (staging a policy
    # before flipping it on).
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


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
    # Shop refresh bookkeeping (web50a): the last time / global turn the per-item
    # stock was restocked to its stock_per_refresh. NULL = the refresh clock has
    # not started (settings.shop.refresh_mode == "none" or never observed). See
    # services/boardgame_shop.maybe_refresh_shop.
    shop_refreshed_at = Column(DateTime, nullable=True)
    shop_refreshed_turn = Column(Integer, nullable=True)
    # web61a: the scheduled wall-clock time of the NEXT time-based shop restock
    # (refresh_mode 'hours'/'days'). Lets refresh_random jitter each interval to
    # an unpredictable moment while staying stable between reads. NULL = the
    # time-based clock has not started (turns mode / no cadence uses the
    # shop_refreshed_* markers instead). See boardgame_shop.maybe_refresh_shop.
    shop_next_refresh_at = Column(DateTime, nullable=True)
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
    # Tile-effect stall (web49a): while status == "blocked", roll attempts are
    # consumed without movement until turns_completed reaches this target
    # (services/boardgame_engine._serve_blocked_turn). NULL otherwise.
    blocked_until_turn = Column(Integer, nullable=True)
    # choose_task pending pick (web50a): JSON list of candidate tasks the team
    # must choose between before its current task is (re)assigned —
    # [{"index", "label", "task_id", "difficulty"}]. Resolved by
    # POST /events/{id}/board/choice. NULL when no choice is pending.
    pending_choice = Column(Text, nullable=True)
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
    stock = Column(Integer, nullable=True)  # NULL = unlimited (current remaining)
    # Per-event OVERRIDE semantics (web50a): a row exists only to override the
    # catalog defaults for this event — an item with NO row is sold at catalog
    # defaults (enabled, list price, unlimited, uncapped). ``enabled`` False
    # hides the item; ``stock_per_refresh`` is the stock granted each refresh
    # (NULL = unlimited, never depletes); ``per_team_cap`` is the max lifetime
    # purchases one team may make of this item (NULL = uncapped).
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    stock_per_refresh = Column(Integer, nullable=True)
    per_team_cap = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class EventTeamInventory(Base):
    """A team-held power-up (web45a) — the ``FeatureActivation`` analogue.
    One row per purchased copy; ``used_turn``/``used_on`` record consumption."""

    __tablename__ = "web_event_team_inventory"
    __table_args__ = (
        Index("idx_web_evt_inv_team", "event_id", "team_id", "status"),
        # Per-team purchase-cap counting (web50a): count copies of one catalog
        # item a team has bought this event.
        Index("idx_web_evt_inv_item", "event_id", "team_id", "shop_item_id"),
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
