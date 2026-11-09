"""Discord server audit log ORM models (web161a).

A Discord server can turn on an audit channel: deleted and edited messages,
joins, leaves, kicks, bans, timeouts, nickname and role changes are posted
there for its admins. Discord only says WHICH message was deleted, never what
it said, so the bot keeps its own short-lived copy of human messages.

Two tables:
    DiscordAuditConfig - one row per Discord server: on/off, the audit
                         channel, per-event switches, ignored channels and
                         how long stored messages are kept. ``group_id`` is
                         for when clans can turn this on for their own
                         server; the main DropTracker server leaves it NULL.
    DiscordMessageLog  - the stored copy of each human message in a server
                         with auditing on. Purged after the server's
                         retention period (services/discord_audit.py).

Only moderation uses these rows: they are never shown on the site or
returned by any API. See the "Moderation logging" section of /privacy.
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)

from .base import Base


class DiscordAuditConfig(Base):
    __tablename__ = "discord_audit_configs"

    guild_id = Column(String(32), primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.group_id", ondelete="SET NULL"), nullable=True)
    enabled = Column(Boolean, nullable=False, default=False)
    channel_id = Column(String(32), nullable=True)
    #: {event kind: bool} overrides of discord_audit_core.DEFAULT_EVENTS.
    events = Column(JSON, nullable=True)
    #: Channel ids (str) whose messages are neither stored nor logged.
    ignored_channel_ids = Column(JSON, nullable=True)
    retention_days = Column(Integer, nullable=False, default=30)
    #: Discord id of whoever last changed the settings.
    updated_by = Column(String(32), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class DiscordMessageLog(Base):
    __tablename__ = "discord_message_log"
    __table_args__ = (
        Index("idx_discord_message_log_guild_created", "guild_id", "created_at"),
        Index("idx_discord_message_log_author", "author_id"),
    )

    message_id = Column(BigInteger, primary_key=True, autoincrement=False)
    guild_id = Column(BigInteger, nullable=False)
    channel_id = Column(BigInteger, nullable=False)
    author_id = Column(BigInteger, nullable=False)
    author_name = Column(String(100), nullable=True)
    content = Column(Text, nullable=True)
    #: [{filename, content_type, size, url, path}] - ``path`` is the private
    #: on-disk copy of an image (relative to the audit file root), or null.
    attachments = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False)
    edited_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True)
