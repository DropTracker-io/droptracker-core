"""Hardcore Worlds: mirror tables, kept apart from the main game.

Hardcore Worlds (launched 2026-11-04) are fresh one-life characters on their
own worlds with their own economy; RuneLite flags them
``WorldType.HARDCORE_MAIN`` and the plugin (6.1.19+) tags their submissions
``world_type=HARDCORE_MAIN``, which intake folds into ``hardcore``.

A hardcore character is a ``players`` row with ``world = 'hardcore'``
(web151a), created by account hash without Wise Old Man
(data.submissions.common.ensure_hardcore_player). It has no
``user_group_association`` rows: its groups are those of the main accounts of
the Discord user who claimed it, and only groups that turned
``track_hardcore_worlds`` on (common.get_player_groups_with_global). The
global group never sees it.

Submissions run through the normal processors with ``world_type="hardcore"``,
exactly like seasonal ones, and land in these mirrors of the seasonal tables;
deaths, diaries and slayer tasks share their main tables via a ``world_type``
column, as seasonal does. Drops keep the CLIENT's price: our item values are
main-game GE prices, which say nothing about the hardcore economy.

``HardcoreSubmission`` is every submission exactly as it arrived (auth key
stripped), written before processing, so nothing is lost if a processor
rejects it or a feature is built later.
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

from .base import Base

# BIGINT in MariaDB, INTEGER under the unit tests' SQLite (autoincrement only
# works on an INTEGER PRIMARY KEY there).
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


def _partition_of_now():
    from datetime import datetime
    now = datetime.now()
    return now.year * 100 + now.month


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
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    # Canonical type (data/submissions/dispatch.py), e.g. "drop", "death".
    submission_type = Column(String(32), nullable=False)
    # Plugin GUID; unique, so a replayed submission is archived once.
    unique_id = Column(String(255), nullable=True, unique=True)
    payload = Column(JSON, nullable=False)
    plugin_version = Column(String(20), nullable=True)
    # Server ACCEPT time (data.submissions.common.received_at).
    date_added = Column(DateTime, nullable=False, default=func.now())
    # YYYYMM of date_added, like drops.partition.
    partition = Column(Integer, nullable=False)


class HardcoreDrop(Base):
    """Mirror of ``seasonal_drops``; ``value`` is the client's price."""
    __tablename__ = "hardcore_drops"
    __table_args__ = {"extend_existing": True}

    drop_id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("items.item_id"), index=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True, nullable=False)
    date_added = Column(DateTime, index=True, default=func.now())
    npc_id = Column(Integer, ForeignKey("npc_list.npc_id"), index=True)
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    value = Column(BigInteger)
    quantity = Column(Integer)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    authed = Column(Boolean, default=False)
    used_api = Column(Boolean, default=False)
    hidden = Column(Boolean, default=False, nullable=False, server_default="0")
    partition = Column(Integer, default=_partition_of_now, index=True)
    unique_id = Column(String(255), nullable=True)
    kill_count = Column(Integer, nullable=True)


class HardcorePersonalBestEntry(Base):
    __tablename__ = "hardcore_personal_best"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True)
    npc_id = Column(Integer, ForeignKey("npc_list.npc_id"))
    kill_time = Column(Integer, nullable=False)
    personal_best = Column(Integer, nullable=False)
    team_size = Column(String(15), nullable=False, default="Solo")
    new_pb = Column(Boolean, default=False)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, nullable=True, default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True)


class HardcoreCollectionLogEntry(Base):
    __tablename__ = "hardcore_collection"
    __table_args__ = {"extend_existing": True}

    log_id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, index=True, nullable=False)
    npc_id = Column(Integer, ForeignKey("npc_list.npc_id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True, nullable=False)
    reported_slots = Column(Integer)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True)


class HardcoreCombatAchievementEntry(Base):
    __tablename__ = "hardcore_combat_achievement"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True)
    task_name = Column(String(255), nullable=False)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True)


class HardcorePlayerPet(Base):
    __tablename__ = "hardcore_player_pets"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True)
    item_id = Column(Integer, ForeignKey("items.item_id"))
    date_added = Column(DateTime, nullable=True)
    unique_id = Column(String(255), nullable=True)
    pet_name = Column(String(255), nullable=False)


class HardcoreQuestCompletionEntry(Base):
    __tablename__ = "hardcore_quest_completions"
    __table_args__ = {"extend_existing": True}

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), index=True, nullable=False)
    quest_name = Column(String(255), nullable=False)
    quests_completed = Column(Integer, nullable=True)
    total_quests = Column(Integer, nullable=True)
    completion_percentage = Column(String(20), nullable=True)
    quest_points = Column(Integer, nullable=True)
    total_quest_points = Column(Integer, nullable=True)
    qp_percentage = Column(String(20), nullable=True)
    timestamp = Column(Integer, nullable=True)
    image_url = Column(String(300), nullable=True)
    video_url = Column(String(500), nullable=True)
    date_added = Column(DateTime, index=True, default=func.now())
    used_api = Column(Boolean, default=False)
    unique_id = Column(String(255), nullable=True, unique=True, index=True)
