"""Conquest event kind (web120a) — the territory map and its live state.

A Conquest map is a set of **regions**, each holding **tiles** (a boss or an
activity). Every tile carries **rules**: an ordinary ``web_event_tasks`` row
plus a troop yield, so the existing task engine decides what counts and
``services/conquest.py`` decides what a troop does. See that module for the
game rules.

Live state sits on the tile row itself (owner, defense) so one row lock
covers a tile; the ownership history (``web_conquest_holds``) is the source of
truth for hold-time scoring, and every troop is logged in
``web_conquest_battles``.

Deliberately NO foreign keys to ``web_event_teams``: an InnoDB FK check takes
a shared lock on the referenced team row, and the event worker's parallel
apply lanes would then deadlock against the score writes. Team ids here are
plain integers; ``services.conquest_engine.forget_team`` clears a team that is
deleted mid-event (called from ``services.event_team_purge``). Event and tile
FKs cascade on delete.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import func

from .base import Base

# BIGINT in MariaDB; SQLite (the unit tests) only auto-increments INTEGER keys.
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class ConquestMap(Base):
    """One per Conquest event: settings, the optional background art and the
    bookkeeping clocks the lifecycle sweep keys off."""

    __tablename__ = "web_conquest_maps"
    __table_args__ = (
        Index("uq_web_conquest_map_event", "event_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    # JSON; services.conquest.conquest_settings() fills every default.
    settings = Column(Text, nullable=True)
    background_url = Column(String(255), nullable=True)
    bg_width = Column(Integer, nullable=True)
    bg_height = Column(Integer, nullable=True)
    # Which built-in map the designer started from (e.g. "gielinor"), if any.
    preset = Column(String(40), nullable=True)
    # The coordinate space tile/region ``shape`` paths are drawn in (web121a),
    # kept apart from the background's size so replacing or removing the art
    # never shifts the territories.
    shape_width = Column(Integer, nullable=True)
    shape_height = Column(Integer, nullable=True)
    # Bumped by every designer save; a save carrying an older revision is
    # refused so two open editors can't silently overwrite each other.
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    # Set once the starting state was dealt at activation (idempotency marker).
    seeded_at = Column(DateTime, nullable=True)
    # Last time the sweep materialized team scores from the holds.
    settled_at = Column(DateTime, nullable=True)
    # Last periodic "map update" post (settings.summary_hours cadence).
    summary_at = Column(DateTime, nullable=True)
    # The phase whose start was last announced (settings.phase_count, web123a).
    phase_announced = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class ConquestRegion(Base):
    """A named group of tiles. Holding every (normal) tile in it is region
    control, worth ``bonus`` (per hour held, or at the end — see settings).
    ``owner_team_id`` caches who controls it now (announcements + display);
    scoring re-derives control from the tiles."""

    __tablename__ = "web_conquest_regions"
    __table_args__ = (
        Index("idx_web_conquest_region_event", "event_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    name = Column(String(60), nullable=False)
    color = Column(String(7), nullable=True)  # "#rrggbb"; NULL = palette default
    bonus = Column(Float, nullable=False, default=0, server_default="0")
    sort = Column(Integer, nullable=False, default=0, server_default="0")
    # Fractional (0..1) label anchor on the map; NULL = centre of its tiles.
    label_x = Column(Float, nullable=True)
    label_y = Column(Float, nullable=True)
    owner_team_id = Column(Integer, nullable=True)  # no FK on purpose (module doc)
    owner_since = Column(DateTime, nullable=True)
    # Outline of the whole region as an SVG path in the map's shape space
    # (web121a); NULL on hand-built maps, which draw soft blobs instead.
    shape = Column(Text, nullable=True)
    # 1 = the contested centre (web123a): its bonus and its tiles' values are
    # multiplied by settings.contested_multiplier.
    contested = Column(Integer, nullable=False, default=0, server_default="0")


class ConquestTile(Base):
    """One territory. ``x``/``y`` are fractional (0..1) positions on the map
    so the overlay scales with any render size. ``kind`` is
    services.conquest.TILE_KINDS. The live state (owner, defense) is on the
    row so the apply path locks exactly one row per tile."""

    __tablename__ = "web_conquest_tiles"
    __table_args__ = (
        Index("idx_web_conquest_tile_event", "event_id", "idx"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    region_id = Column(Integer, ForeignKey("web_conquest_regions.id", ondelete="SET NULL"),
                       nullable=True)
    idx = Column(Integer, nullable=False, default=0)  # stable display order
    label = Column(String(80), nullable=False)
    x = Column(Float, nullable=False, default=0.5)
    y = Column(Float, nullable=False, default=0.5)
    kind = Column(String(16), nullable=False, default="normal", server_default="normal")
    # Points per hour held (hold_time scoring) or at the end (final scoring).
    value = Column(Float, nullable=False, default=1, server_default="1")
    # Icon: an NPC portrait (/img/npcdb) or an item (/img/itemdb); item wins.
    icon_npc_id = Column(Integer, nullable=True)
    icon_item_id = Column(Integer, nullable=True)
    # The territory this tile covers, an SVG path in the map's shape space
    # (web121a). NULL = drawn as a badge only.
    shape = Column(Text, nullable=True)
    # Organiser overrides (web122a): this tile's defense cap and the garrison
    # it starts with when unowned; NULL = the map's max_defense /
    # neutral_defense.
    max_defense = Column(Integer, nullable=True)
    garrison = Column(Integer, nullable=True)
    # The team that starts here in the "homes" start mode, and re-enters here
    # after losing every tile. No FK on purpose (module doc).
    home_team_id = Column(Integer, nullable=True)
    # --- live state ---
    owner_team_id = Column(Integer, nullable=True)  # no FK on purpose (module doc)
    defense = Column(Integer, nullable=False, default=0, server_default="0")
    owner_since = Column(DateTime, nullable=True)
    captures = Column(Integer, nullable=False, default=0, server_default="0")
    last_battle_at = Column(DateTime, nullable=True)
    # Troops thrown at this tile by the current siege (web123a): with no dice,
    # every other troop against an underdog is absorbed. Reset on capture.
    siege = Column(Integer, nullable=False, default=0, server_default="0")


class ConquestRule(Base):
    """What earns troops on a tile: every time a team's progress on ``task``
    crosses another multiple of its target, the team gets ``troops`` troops
    here. A task backs at most one rule (unique), so a submission's troops
    land on exactly one tile per task."""

    __tablename__ = "web_conquest_rules"
    __table_args__ = (
        Index("uq_web_conquest_rule_task", "task_id", unique=True),
        Index("idx_web_conquest_rule_tile", "tile_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    tile_id = Column(Integer, ForeignKey("web_conquest_tiles.id", ondelete="CASCADE"),
                     nullable=False)
    task_id = Column(Integer, ForeignKey("web_event_tasks.id", ondelete="CASCADE"),
                     nullable=False)
    troops = Column(Integer, nullable=False, default=1, server_default="1")
    sort = Column(Integer, nullable=False, default=0, server_default="0")
    # 1 = a one-time award: pays only the first time the target is reached,
    # like an achievement (web122a).
    once = Column(Integer, nullable=False, default=0, server_default="0")
    # 0 = plays in every phase; N = only in phase N (settings.phase_count,
    # web123a).
    phase = Column(Integer, nullable=False, default=0, server_default="0")


class ConquestEdge(Base):
    """Two tiles that border each other (undirected, ``tile_a_id`` <
    ``tile_b_id``). The fronts rule reads them: a team's troops only count on
    its own tiles and the ones bordering them (services.conquest)."""

    __tablename__ = "web_conquest_edges"
    __table_args__ = (
        Index("uq_web_conquest_edge", "event_id", "tile_a_id", "tile_b_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    tile_a_id = Column(Integer, ForeignKey("web_conquest_tiles.id", ondelete="CASCADE"),
                       nullable=False)
    tile_b_id = Column(Integer, ForeignKey("web_conquest_tiles.id", ondelete="CASCADE"),
                       nullable=False)


class ConquestHold(Base):
    """One stretch of one team owning one tile; ``ended_at`` NULL = held
    now. At most one open row per tile (written under the tile lock). The
    hold-time score is computed from these, never accumulated."""

    __tablename__ = "web_conquest_holds"
    __table_args__ = (
        Index("idx_web_conquest_hold_event", "event_id", "tile_id"),
        {"extend_existing": True},
    )

    id = Column(_BIG_PK, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    tile_id = Column(Integer, ForeignKey("web_conquest_tiles.id", ondelete="CASCADE"),
                     nullable=False)
    team_id = Column(Integer, nullable=False)  # no FK on purpose (module doc)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)


class ConquestBattle(Base):
    """Every troop, whatever it did (services.conquest.OUTCOMES): the battle
    log, the source of the Discord posts and the answer to "why did we lose
    Vorkath?". ``completion_id``/``task_id``/``player_id`` name the ledger row
    that earned the troop (plain ints — the ledger may be purged)."""

    __tablename__ = "web_conquest_battles"
    __table_args__ = (
        Index("idx_web_conquest_battle_event", "event_id", "id"),
        Index("idx_web_conquest_battle_tile", "tile_id", "id"),
        {"extend_existing": True},
    )

    id = Column(_BIG_PK, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    tile_id = Column(Integer, ForeignKey("web_conquest_tiles.id", ondelete="CASCADE"),
                     nullable=False)
    team_id = Column(Integer, nullable=True)  # the team the troop belonged to
    outcome = Column(String(16), nullable=False)
    owner_before = Column(Integer, nullable=True)
    owner_after = Column(Integer, nullable=True)
    defense_before = Column(Integer, nullable=False, default=0)
    defense_after = Column(Integer, nullable=False, default=0)
    # Dice faces, highest first, comma-separated ("6,3"); NULL when no roll.
    attack_dice = Column(String(16), nullable=True)
    defense_dice = Column(String(16), nullable=True)
    player_id = Column(Integer, nullable=True)
    completion_id = Column(BigInteger, nullable=True)
    task_id = Column(Integer, nullable=True)
    # 'troop' (earned in play), 'admin' (a manual correction) or 'rule' (a
    # game rule acting on its own: a retreat).
    source = Column(String(16), nullable=False, default="troop", server_default="troop")
    # Bonus points this troop won (a bounty for a capture, web123a); added to
    # the team's score in both scoring modes.
    points = Column(Float, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=func.now(), nullable=False)


class ConquestTroops(Base):
    """Per (tile, team) troop book: how many troops the team has earned there
    and any troop debt (and what it earned out of reach). A revoked submission can't un-roll dice, so troops it
    had already spent become debt that the team's next troops on that tile pay
    off first."""

    __tablename__ = "web_conquest_troops"
    __table_args__ = (
        Index("uq_web_conquest_troops", "tile_id", "team_id", unique=True),
        Index("idx_web_conquest_troops_event", "event_id", "team_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    tile_id = Column(Integer, ForeignKey("web_conquest_tiles.id", ondelete="CASCADE"),
                     nullable=False)
    team_id = Column(Integer, nullable=False)  # no FK on purpose (module doc)
    earned = Column(Integer, nullable=False, default=0, server_default="0")
    debt = Column(Integer, nullable=False, default=0, server_default="0")
    # Troops earned while the tile was out of the team's reach (fronts,
    # web122a). With out_of_reach = "ignore" they never fight; kept so a
    # later policy can deploy them.
    held = Column(Integer, nullable=False, default=0, server_default="0")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)


class ConquestHotZone(Base):
    """A region that is "hot" for a while (web123a): troops earned on its
    tiles count double (services.conquest.HOT_MULTIPLIER). Planned in the
    designer before the event starts; ``announced_at`` marks the Discord
    post when it went hot."""

    __tablename__ = "web_conquest_hotzones"
    __table_args__ = (
        Index("idx_web_conquest_hotzone_event", "event_id", "starts_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    region_id = Column(Integer, ForeignKey("web_conquest_regions.id", ondelete="CASCADE"),
                       nullable=False)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    announced_at = Column(DateTime, nullable=True)


class ConquestTeamState(Base):
    """Per-team comeback bookkeeping (web123a). ``landless_since`` is set when
    a team loses its last tile; when it takes a tile again it gets a shield
    (``shield_until``: its tiles can't be attacked) or a boost
    (``boost_until``: its troops count double), per settings.comeback."""

    __tablename__ = "web_conquest_team_state"
    __table_args__ = (
        Index("uq_web_conquest_team_state", "event_id", "team_id", unique=True),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("web_events.id", ondelete="CASCADE"),
                      nullable=False)
    team_id = Column(Integer, nullable=False)  # no FK on purpose (module doc)
    landless_since = Column(DateTime, nullable=True)
    shield_until = Column(DateTime, nullable=True)
    boost_until = Column(DateTime, nullable=True)
    comebacks = Column(Integer, nullable=False, default=0, server_default="0")
