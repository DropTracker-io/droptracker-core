"""Notification footers (web140a): what a group puts under its bot messages.

Without a row every notification carries the DropTracker footer
(``DISCORD_MESSAGE_FOOTER``). A row with ``notification_type`` "" is the
group's choice for every type; a row naming a type overrides that choice for
that type alone. ``mode`` is ``default`` (the DropTracker footer), ``none`` or
``custom`` (``text``, which may carry the type's placeholders).

Read through services/notification_text.resolve_footer, which only honours
rows while the group holds the ``custom_footer`` entitlement. Rows survive a
lapse so re-subscribing restores them.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from .base import Base


class GroupNotificationFooter(Base):
    __tablename__ = "group_notification_footers"
    __table_args__ = (
        UniqueConstraint("group_id", "notification_type", name="uq_group_notification_footer"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=False)
    # "" = every notification type.
    notification_type = Column(String(16), nullable=False, default="")
    mode = Column(String(8), nullable=False, default="default")
    text = Column(String(2048), nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    updated_by = Column(Integer, nullable=True)
