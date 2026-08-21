"""Account *state* — what a player currently has, as opposed to what happened.

Everything else in this schema is an event stream: ``drops``, ``collection``,
``combat_achievement`` and friends each record the moment something occurred.
That answers "what did they get last week?" but cannot answer "what do they have
now?", because anything obtained before the plugin was installed never produced
an event. A collection log page, a combat achievement tier, a quest list and any
"who has the most slots?" leaderboard all need current state.

These tables are therefore **idempotent upserts, not appends**: one row per
player per item / quest / diary tier, rewritten in place by each sync. Re-syncing
the same snapshot twice changes nothing, which is what makes the sync endpoint
safe to retry.

Growth is bounded by design — a player can only have so many collection log
slots, quests and diary tiers, so these tables converge rather than accumulate.
The one genuinely unbounded thing (per-skill XP over time) is deliberately NOT
here: it needs a retention policy first, and ``player_exp`` already holds the
latest values.

Sizing note before this ships to production: ``player_clog_items`` is up to
~1,500 rows per player who opens their collection log. Do the arithmetic against
the real player count — the dev box carries a 7-day slice and will understate it.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)

from .base import Base


class PlayerState(Base):
    """One row per player: the header for everything else here.

    Also the record of whether we have *ever* had a successful sync, which the
    diff engine needs — a player with no row is being seen for the first time,
    and must not be told that all 900 of their collection log items are new.
    """

    __tablename__ = "player_state"
    __table_args__ = ({"extend_existing": True},)

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    # Raw IRONMAN varbit value rather than a decoded label, so a new account type
    # does not need a migration to be storable.
    account_type = Column(SmallInteger, nullable=True)
    combat_level = Column(SmallInteger, nullable=True)
    # Collection log progress as the game itself reports it. Kept alongside the
    # per-item rows because the game knows the true total (including items we
    # have no row for), which is what a "412/1,584" display needs.
    clog_slots = Column(Integer, nullable=True)
    clog_slots_total = Column(Integer, nullable=True)
    # Which manifest the client was using. When a sync looks wrong, the first
    # question is always "which varp list did that client read?".
    manifest_version = Column(String(32), nullable=True)
    # What triggered the sync (login, interval, clog-open, ...) — the difference
    # between a routine sync and a suspicious one is usually here.
    last_sync_source = Column(String(32), nullable=True)
    # Fingerprint of the character model we currently hold for this player.
    # Lets the renderer find the right file without listing a directory, and
    # tells the upload endpoint whether it already has this outfit.
    model_fingerprint = Column(String(32), nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class PlayerCollectionLogItem(Base):
    """Every collection log slot a player has filled, with its quantity.

    ``first_seen_at`` is when *we* first recorded the item, not when the player
    obtained it — a full scrape backfills items unlocked years ago. It must never
    be presented as an obtained date.
    """

    __tablename__ = "player_clog_items"
    __table_args__ = ({"extend_existing": True},)

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    item_id = Column(Integer, primary_key=True)
    quantity = Column(Integer, nullable=False, default=0)
    first_seen_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class PlayerCombatAchievementVarps(Base):
    """Raw combat achievement completion bits, exactly as the client read them.

    Stored raw and decoded on read, deliberately. The varp list comes from the
    manifest and grows as Jagex appends varps, so decoding at write time would
    bake whatever task registry we had that day into the stored data — and a
    later registry fix could not be applied retroactively. Raw bits stay true.
    """

    __tablename__ = "player_ca_varps"
    __table_args__ = ({"extend_existing": True},)

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    # JSON object: {"3116": 123456, "3117": 0, ...}. Text for the same reason as
    # the rest of the codebase's JSON columns — we never query into it.
    varps = Column(Text, nullable=False)
    # Denormalised so leaderboards and profile headers do not decode every row.
    tasks_completed = Column(Integer, nullable=True)
    # JSON array of the task varbits reported complete, when the client had a
    # task registry to read. Enables the per-boss breakdown the in-game
    # interface shows; the raw varps above remain the complete truth, including
    # tasks the registry has no entry for.
    completed_tasks = Column(Text, nullable=True)
    points = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class PlayerQuestState(Base):
    """Per-quest status: 0 not started, 1 in progress, 2 finished."""

    __tablename__ = "player_quest_states"
    __table_args__ = ({"extend_existing": True},)

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    quest_id = Column(Integer, primary_key=True)
    state = Column(SmallInteger, nullable=False, default=0)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class PlayerDiaryTier(Base):
    """Completed task count for one achievement diary area and tier.

    A count rather than a boolean because partial progress is the interesting
    part; "complete" is ``completed_count >= the tier's task count``, and that
    total is reference data, not per-player state.
    """

    __tablename__ = "player_diary_tiers"
    __table_args__ = ({"extend_existing": True},)

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    area_id = Column(Integer, primary_key=True)
    tier = Column(SmallInteger, primary_key=True)
    completed_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
