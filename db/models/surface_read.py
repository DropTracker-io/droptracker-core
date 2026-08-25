"""Per-user read pointers for non-chat conversation surfaces (web102a).

``chat_reads`` covers chat threads; this table covers the surfaces that keep
their own message tables — tickets (``ticket_messages.id``) and suggestions
(``suggestion_messages.id``) — so the site's unified inbox can badge them.

Advance-only, like ``chat_reads``. Crucially, readers must treat the floor as
``max(pointer, the user's own latest message id in that surface)`` — a reply
(from the site OR mirrored from Discord) proves the author was caught up at
that point, which is what lets a Discord-side reply clear a site badge without
any explicit read report. See ``services/inbox.py``.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func

from .base import Base

#: Surfaces with their own message tables. Chat kinds use chat_reads instead.
SURFACE_READ_SURFACES = ("ticket", "suggestion")


class SurfaceRead(Base):
    """Highest message id one user has seen on one ticket/suggestion."""

    __tablename__ = "surface_reads"
    __table_args__ = ({"extend_existing": True},)

    surface = Column(String(16), primary_key=True)
    # tickets.ticket_id or suggestions.id depending on surface. Not a foreign
    # key for the same reason chat_participants.party_id is not: the referent
    # depends on the discriminator. Writers validate.
    ref_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), primary_key=True)
    last_read_message_id = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )
