"""Per-group always-announce list — items and NPCs a clan always wants posted.

The inverse of ``group_notification_blacklist``: a leader adds an **item**
("Twisted ancestral colour kit") or an **NPC** and drops of that item — or from
that NPC — are announced in the group's Discord even when they fall below the
group's ``minimum_value_to_notify``. This exists for the "notable" items the
plugin already force-screenshots (untradeable kits, pieces, dyes) that carry no
GE value and would otherwise never clear the value threshold.

It widens the drop announcement gate and nothing else: no extra points, no
board changes, and the group's other rules still apply — a screenshot
requirement still withholds an imageless drop, and the blacklist wins outright
when a name is on both lists (``create_notification`` applies it last).

Matching is by normalized name for the same reason the blacklist's is: payloads
carry names, and one physical item arrives under several ids. ``match_key``
holds the normalized form (see ``db.notification_always_list``); ``game_id``
is carried purely so the web UI can render the itemdb/npcdb icon.
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

from .base import Base


class GroupNotificationAlwaysList(Base):
    __tablename__ = "group_notification_always_list"
    __table_args__ = (
        # One row per (group, kind, normalized name): re-adding "Twisted Bow"
        # after "twisted bow" is an update, never a duplicate.
        UniqueConstraint(
            "group_id", "entry_type", "match_key", name="uix_group_always_list_entry"
        ),
        Index("idx_group_always_list_group", "group_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    # 'item' or 'npc' — see db.notification_always_list.ENTRY_TYPES.
    entry_type = Column(String(8), nullable=False)
    # What the leader picked, preserved for display exactly as chosen.
    entry_name = Column(String(125), nullable=False)
    # Normalized form the pipeline matches on (same rules as the blacklist).
    match_key = Column(String(125), nullable=False)
    # item_id / npc_id when the entry came from the picker; NULL when typed in.
    game_id = Column(Integer, nullable=True)
    added_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    date_added = Column(DateTime, default=func.now())
    date_updated = Column(DateTime, onupdate=func.now(), default=func.now())
