"""Per-group Components V2 layouts for notifications.

Parallel to ``group_embeds``, not a replacement for it. A group either has a row
here with ``active`` set for a notification type — in which case that type sends
as components — or it does not, and the existing embed path runs unchanged.
Discord will not accept both in one message anyway, so the choice is per type
and genuinely either/or.

Keeping this in its own table rather than adding columns to ``group_embeds``
means a group can author and preview a components layout while still sending
embeds, and switch over (or back) without losing either version.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.mysql import LONGTEXT

from .base import Base


class GroupComponentLayout(Base):
    __tablename__ = "group_component_layouts"
    __table_args__ = ({"extend_existing": True},)

    group_id = Column(Integer, ForeignKey("groups.group_id"), primary_key=True)
    # "pb", "drop", "clog", ... matching the embed template types.
    notification_type = Column(String(32), primary_key=True)
    # The layout document; see services/component_layout.py for its shape.
    # LONGTEXT because a rich layout with several long text blocks passes
    # TEXT's 64KB ceiling more easily than it looks.
    layout = Column(LONGTEXT, nullable=False)
    # False means "authored but not live" — the editor can be used without
    # changing what the group's members actually receive.
    active = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
