"""Hardcore Worlds: everything submitted from them, kept apart from the main game.

Hardcore Worlds (launched 2026-11-04) are fresh one-life characters on their
own worlds with their own economy; RuneLite flags them
``WorldType.HARDCORE_MAIN`` and the plugin (6.1.19+) tags their submissions
``world_type=HARDCORE_MAIN``. None of it may reach the main-game tables: a
hardcore character is not the main account, and its loot is priced by a
different Grand Exchange.

So these tables stand alone, like the PvP ones:

* ``HardcorePlayer`` is the character, by account hash. It is never a
  ``players`` row: main player creation resolves identity through Wise Old
  Man and joins groups, neither of which applies to a fresh character.
* ``HardcoreSubmission`` is every submission as it arrived (auth key
  stripped), one row per plugin GUID. It is the source of truth: typed tables
  and boards can be built from it later without having lost anything.
* ``HardcoreDrop`` is the typed copy of drops. Values are the CLIENT's price,
  not ours: our item values are main-game GE prices, which say nothing about
  the hardcore economy.

Item and NPC ids are plain columns rather than foreign keys, so intake never
creates catalog rows on these worlds' say-so.

Written by ``data/submissions/hardcore.py``; nothing here feeds Redis, the
leaderboards, clan points, events or Discord notifications.
"""
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base

# BIGINT in MariaDB, INTEGER under the unit tests' SQLite (autoincrement only
# works on an INTEGER PRIMARY KEY there).
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class HardcorePlayer(Base):
    __tablename__ = "hardcore_players"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_hash = Column(String(100), nullable=False, unique=True)
    # Latest name the character submitted under.
    player_name = Column(String(64), nullable=False, index=True)
    account_type = Column(String(32), nullable=True)
    plugin_version = Column(String(20), nullable=True)
    first_seen = Column(DateTime, nullable=False, default=func.now())
    last_seen = Column(DateTime, nullable=False, default=func.now())


class HardcoreSubmission(Base):
    __tablename__ = "hardcore_submissions"
    __table_args__ = (
        Index("ix_hardcore_submissions_player_type_date",
              "player_id", "submission_type", "date_added"),
        Index("ix_hardcore_submissions_type_date", "submission_type", "date_added"),
        Index("ix_hardcore_submissions_partition", "partition"),
        {"extend_existing": True},
    )

    id = Column(_BIG_PK, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("hardcore_players.id"), nullable=False)
    # Canonical type (data/submissions/dispatch.py), e.g. "drop", "death".
    submission_type = Column(String(32), nullable=False)
    # Plugin GUID; unique, so a replayed submission is a no-op.
    unique_id = Column(String(255), nullable=True, unique=True)
    payload = Column(JSON, nullable=False)
    image_url = Column(String(300), nullable=True)
    plugin_version = Column(String(20), nullable=True)
    used_api = Column(Boolean, nullable=False, default=False)
    # Server ACCEPT time (data.submissions.common.received_at).
    date_added = Column(DateTime, nullable=False, default=func.now())
    # YYYYMM of date_added, like drops.partition.
    partition = Column(Integer, nullable=False)

    player = relationship("HardcorePlayer")


class HardcoreDrop(Base):
    __tablename__ = "hardcore_drops"
    __table_args__ = (
        Index("ix_hardcore_drops_player_date", "player_id", "date_added"),
        Index("ix_hardcore_drops_partition", "partition"),
        Index("ix_hardcore_drops_item", "item_id"),
        {"extend_existing": True},
    )

    id = Column(_BIG_PK, primary_key=True, autoincrement=True)
    submission_id = Column(_BIG_PK, ForeignKey("hardcore_submissions.id", ondelete="CASCADE"),
                           nullable=False, unique=True)
    player_id = Column(Integer, ForeignKey("hardcore_players.id"), nullable=False)
    item_id = Column(Integer, nullable=True)
    item_name = Column(String(255), nullable=False)
    npc_name = Column(String(255), nullable=True)
    quantity = Column(Integer, nullable=False)
    # Per-item price as the client valued it, and value x quantity. BigInteger:
    # a max cash stack overflows int.
    unit_value = Column(BigInteger, nullable=False, default=0)
    total_value = Column(BigInteger, nullable=False, default=0)
    kill_count = Column(Integer, nullable=True)
    image_url = Column(String(300), nullable=True)
    date_added = Column(DateTime, nullable=False, default=func.now())
    partition = Column(Integer, nullable=False)

    player = relationship("HardcorePlayer")
    submission = relationship("HardcoreSubmission")
