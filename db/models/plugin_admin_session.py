"""Plugin group-admin device sessions (web142a).

What the RuneLite plugin holds after a group's verification code was entered
in game: a bearer token for administering that one group from that one game
account. Only a SHA-256 of the token is stored. The token is 32 random bytes,
so a plain hash is enough; it cannot be brute-forced the way a nine-digit
code could.

A session is good only while all of these still hold (checked on every call
by ``db/plugin_admin_sessions.py``): it was minted under the group's current
code ``generation`` (a reset or clear revokes every session at once), the
request comes from the same account hash, that account still belongs to the
same site user, and that user still owns or administers the group.
"""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String

from .base import Base


class PluginAdminSession(Base):
    __tablename__ = "plugin_admin_sessions"
    __table_args__ = (
        Index("ix_plugin_admin_sessions_group", "group_id"),
        Index("ix_plugin_admin_sessions_account", "account_hash"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    group_id = Column(Integer, ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, nullable=False)
    account_hash = Column(String(100), nullable=False)
    #: The group's code generation this was minted under.
    generation = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    #: 'unlink' | 'relink' | 'expired'.
    revoked_reason = Column(String(16), nullable=True)
