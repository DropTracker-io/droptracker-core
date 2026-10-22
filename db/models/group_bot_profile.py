"""Custom bot profile (web139a): the core bot's look inside one group's server.

One row per group. The ``nick``/``bio``/``avatar_url``/``banner_url`` columns
are what the group asked for; the ``applied_*`` columns are what the core bot
last told Discord. services/bot_profile.py diffs the two (with "nothing" as
the desired state when the group has lost the ``custom_bot_profile``
entitlement) and PATCHes only what differs. Desired fields survive a lapse so
re-subscribing restores the profile.

``avatar_url``/``banner_url`` point at B2 (``dt_uploads/bot_profiles/``); the
bot downloads them and sends them to Discord as data URIs.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from .base import Base


class GroupBotProfile(Base):
    __tablename__ = "group_bot_profiles"
    __table_args__ = {"extend_existing": True}

    group_id = Column(Integer, ForeignKey("groups.group_id", ondelete="CASCADE"), primary_key=True)
    # The server the profile was last applied in. Kept so a group that moves
    # servers (or is deleted) can still be reverted where it was applied.
    guild_id = Column(String(32), nullable=True)

    nick = Column(String(32), nullable=True)
    bio = Column(String(190), nullable=True)
    avatar_url = Column(String(512), nullable=True)
    banner_url = Column(String(512), nullable=True)

    applied_nick = Column(String(32), nullable=True)
    applied_bio = Column(String(190), nullable=True)
    applied_avatar_url = Column(String(512), nullable=True)
    applied_banner_url = Column(String(512), nullable=True)
    applied_at = Column(DateTime, nullable=True)

    # pending (saved, not sent yet) | applied (custom look live) |
    # default (bot back to its normal look: reset, or subscription ended) | error
    status = Column(String(12), nullable=False, default="pending")
    last_error = Column(String(255), nullable=True)
    # Discord asked us to wait (429); the reconciler skips the row until then.
    retry_after = Column(DateTime, nullable=True)

    updated_by_user_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
