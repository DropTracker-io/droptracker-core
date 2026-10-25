"""PvP kills: loot taken from other players, kept apart from NPC drops.

The plugin reports a player kill as an ordinary ``drop`` bundle with
``source_type="pvp"`` and the victim's name where an NPC name would be.
Before these tables existed that bundle ran through the NPC pipeline, which
rejected it (no NPC by that name), or worse, booked it as a drop from a real
NPC whenever a victim happened to share a name with one. Now
``data/submissions/pvp.py`` lifts the bundle out before it gets that far and
writes it here, one ``PvpKill`` per kill with its items beside it.

Nothing in here feeds the ``drops`` table, the loot leaderboards or clan
points. PvP has its own Redis standings (``services/pvp_standings.py``), its
own lootboard and its own notifications.

Whether a kill counts is decided twice:

* ``PvpKill.excluded_reason`` is the global verdict. A kill in a PvP minigame
  (LMS, Emir's Arena, Clan Wars…) or of the killer's own linked account is
  stored but counts nowhere.
* ``PvpKillGroup`` is each group's verdict, because the rules that decide it
  (are clanmate kills allowed, how many kills of one victim a day) are that
  group's settings. Boards and standings read only ``counted`` rows, so a
  rebuild re-applies exactly what intake decided.
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
)
from sqlalchemy import func
from sqlalchemy.orm import relationship

from .base import Base

# BIGINT in MariaDB, INTEGER under the unit tests' SQLite (autoincrement only
# works on an INTEGER PRIMARY KEY there).
_BIG_PK = BigInteger().with_variant(Integer, "sqlite")


class PvpKill(Base):
    __tablename__ = "pvp_kills"
    __table_args__ = (
        # "This player's kills this month / of this victim today": every
        # standing, board and the same-victim cap reads by killer and date.
        Index("ix_pvp_kills_player_date", "player_id", "date_added"),
        Index("ix_pvp_kills_victim_date", "victim_name", "date_added"),
        Index("ix_pvp_kills_partition", "partition"),
        {"extend_existing": True},
    )

    id = Column(_BIG_PK, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.player_id"), nullable=False)
    # The victim's RSN as the client saw it (12 chars in game; room for
    # display-name oddities). victim_player_id is set when that name is a
    # player we track, so a profile can show "killed by" as well.
    victim_name = Column(String(64), nullable=False)
    victim_player_id = Column(Integer, nullable=True, index=True)
    # Sent by plugin 6.1.9+; NULL from older clients.
    victim_combat_level = Column(SmallInteger, nullable=True)
    world = Column(SmallInteger, nullable=True)
    wilderness_level = Column(SmallInteger, nullable=True)
    region_id = Column(Integer, nullable=True)
    location = Column(String(125), nullable=True)
    # Sum of item values x quantities. BigInteger: a max-cash-stack kill
    # overflows int.
    total_value = Column(BigInteger, nullable=False, default=0)
    item_count = Column(Integer, nullable=False, default=0)
    world_type = Column(String(20), nullable=False, default="main")
    image_url = Column(String(300), nullable=True)
    # GUID of the bundle's first item embed: the plugin mints one per embed,
    # so the first one names the kill. Unique, so a replayed bundle is a no-op.
    unique_id = Column(String(255), nullable=True, unique=True)
    used_api = Column(Boolean, nullable=False, default=False)
    # Server ACCEPT time (data.submissions.common.received_at), not pickup time.
    date_added = Column(DateTime, nullable=False, default=func.now())
    # YYYYMM of date_added, like drops.partition.
    partition = Column(Integer, nullable=False)
    hidden = Column(Boolean, nullable=False, default=False, server_default="0")
    # Global reason this kill counts nowhere ("pvp_minigame:lms",
    # "own_account"); NULL when it counts.
    excluded_reason = Column(String(48), nullable=True)
    plugin_version = Column(String(20), nullable=True)
    # "kill", or "loot_key": a loot key's loot that could not be matched to one
    # kill (web148a). A loot_key row adds loot to the boards, never a kill.
    kind = Column(String(12), nullable=False, default="kill", server_default="kill")
    # Set on a kill when a loot key's loot was matched to it and added to its
    # items; the key's GUID, so a replay is a no-op.
    loot_key_guid = Column(String(255), nullable=True, unique=True)
    loot_key_at = Column(DateTime, nullable=True)

    player = relationship("Player", foreign_keys=[player_id])
    items = relationship(
        "PvpKillItem", back_populates="kill", cascade="all, delete-orphan",
        order_by="PvpKillItem.id",
    )
    groups = relationship(
        "PvpKillGroup", back_populates="kill", cascade="all, delete-orphan",
    )


class PvpKillItem(Base):
    __tablename__ = "pvp_kill_items"
    __table_args__ = {"extend_existing": True}

    id = Column(_BIG_PK, primary_key=True, autoincrement=True)
    kill_id = Column(_BIG_PK, ForeignKey("pvp_kills.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    item_id = Column(Integer, nullable=False, index=True)
    item_name = Column(String(125), nullable=True)
    quantity = Column(Integer, nullable=False, default=1)
    # Per-unit GE value at the time of the kill (what the drops table stores).
    value = Column(BigInteger, nullable=False, default=0)

    kill = relationship("PvpKill", back_populates="items")


class PvpKillGroup(Base):
    """Whether a kill counts toward one group's PvP standings and board.

    One row per group the killer belonged to at intake (the global group 2
    included). ``counted`` False carries a ``reason`` so a leader can see why
    a kill is missing from their board.
    """

    __tablename__ = "pvp_kill_groups"
    __table_args__ = (
        Index("ix_pvp_kill_groups_group_counted", "group_id", "counted", "kill_id"),
        {"extend_existing": True},
    )

    kill_id = Column(_BIG_PK, ForeignKey("pvp_kills.id", ondelete="CASCADE"),
                     primary_key=True)
    group_id = Column(Integer, primary_key=True)
    counted = Column(Boolean, nullable=False, default=True)
    # "member_kill" | "same_victim_limit" when not counted.
    reason = Column(String(32), nullable=True)

    kill = relationship("PvpKill", back_populates="groups")
