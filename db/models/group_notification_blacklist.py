"""Per-group notification blacklist — items and NPCs a clan never wants posted.

A group leader adds an **item** ("Coins", "Bones") or an **NPC** ("Barrows",
"Chambers of Xeric") to this list and the pipeline stops sending that group's
Discord channel any notification whose subject is that item, or whose source is
that NPC. Nothing else changes: the submission is still recorded, still scores
toward events, still counts on the lootboard and the leaderboards — only the
Discord announcement is withheld. That distinction is the whole point of the
feature; it is a *noise* control, not a data control.

Matching is by name, not id, because the notification payloads the pipeline
builds carry names (``item_name`` / ``npc_name`` / ``boss_name`` / ``source``)
and only sometimes an id. ``match_key`` holds the normalized form the pipeline
compares against (see ``db.notification_blacklist.match_key``); ``game_id`` is
carried alongside purely so the web UI can render the itemdb/npcdb icon, and is
nullable for entries typed in by hand.
"""

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from .base import Base


class GroupNotificationBlacklist(Base):
    __tablename__ = "group_notification_blacklist"
    __table_args__ = (
        # One row per (group, kind, normalized name): adding "Twisted Bow"
        # twice — or after "twisted bow" — is an update, never a duplicate.
        UniqueConstraint(
            "group_id", "entry_type", "match_key", name="uix_group_blacklist_entry"
        ),
        Index("idx_group_blacklist_group", "group_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    # 'item' or 'npc' — see db.notification_blacklist.ENTRY_TYPES.
    entry_type = Column(String(8), nullable=False)
    # What the leader picked, preserved for display exactly as chosen.
    entry_name = Column(String(125), nullable=False)
    # Normalized form the pipeline matches on (lowercase, underscores → spaces,
    # whitespace collapsed). Stored rather than computed at read time so the
    # unique constraint and the hot-path comparison agree by construction.
    match_key = Column(String(125), nullable=False)
    # item_id / npc_id when the entry came from the picker; NULL when typed in.
    game_id = Column(Integer, nullable=True)
    added_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())

    group = relationship("Group", back_populates="notification_blacklist")
