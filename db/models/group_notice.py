"""Bot-raised per-group problem notices (web102a).

One row per (group, problem code) — NOT per incident. A recurring failure
("lootboard channel unreachable" every sweep) bumps ``last_raised_at`` /
``raise_count`` on the same row and posts a system entry to the same
long-lived ``group_notice`` chat thread, so a clan's history of
"broke / fixed / broke again" reads as one timeline and the superadmin
console shows one row per problem class rather than a flood.

State machine lives in ``services/group_notices.py`` (raise / resolve /
Redis cooldowns / DM fan-out on the open-transition only). The thread's own
``status`` stays ``open`` when the notice resolves — admins may still ask
questions; ``status`` here is the problem's state, not the conversation's.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

from .base import Base

GROUP_NOTICE_STATUSES = ("open", "resolved")
GROUP_NOTICE_SEVERITIES = ("info", "minor", "major", "critical")


class GroupNotice(Base):
    """One problem class the bot has told one group about."""

    __tablename__ = "group_notices"
    __table_args__ = (
        # get-or-create race safety, same idiom as uq_chat_thread_subject.
        UniqueConstraint("group_id", "code", name="uq_group_notice"),
        Index("idx_group_notice_status", "status", "last_raised_at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    # Machine code for the problem class, e.g. 'notify_channel_forbidden'.
    code = Column(String(48), nullable=False)
    severity = Column(String(16), nullable=False, default="major")
    title = Column(String(200), nullable=False)
    status = Column(String(16), nullable=False, default="open")
    thread_id = Column(Integer, ForeignKey("chat_threads.id"), nullable=True)
    first_raised_at = Column(DateTime, nullable=False, default=func.now())
    last_raised_at = Column(DateTime, nullable=False, default=func.now())
    raise_count = Column(Integer, nullable=False, default=1)
    resolved_at = Column(DateTime, nullable=True)
    # NULL when auto-resolved by the emitter's success path.
    resolved_by_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    # Nouns for the client/system entries: channel ids, notification types, …
    data_json = Column(Text, nullable=True)
