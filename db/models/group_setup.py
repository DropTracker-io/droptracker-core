"""Group setup health (web138a): bot presence history and setup alerts.

``GuildBotEvent`` records the bot joining or leaving a Discord server. Joins
come from the gateway (GuildJoin); leaves are inferred by the core bot's
5-minute ``bot:guilds`` refresh, because interactions.py never delivers a
GuildLeft for this bot. Together they answer "did this group ever have the
bot, and when did it lose it?", which nothing could before.

``GroupSetupAlert`` is one row per (kind, subject): what services/setup_alerts.py
noticed, who it told, and whether it has cleared. A condition that already
held when the detector first saw it is recorded as ``baseline`` and never
messaged (those groups get a one-off, owner-approved campaign instead).
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, UniqueConstraint

from .base import Base


class GuildBotEvent(Base):
    __tablename__ = "guild_bot_events"
    __table_args__ = (
        Index("ix_guild_bot_events_guild", "guild_id", "at"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String(32), nullable=False)
    event = Column(String(8), nullable=False)  # join | leave
    source = Column(String(16), nullable=False)  # gateway | sweep
    at = Column(DateTime, nullable=False, default=datetime.now)


class GroupSetupAlert(Base):
    __tablename__ = "group_setup_alerts"
    __table_args__ = (
        UniqueConstraint("kind", "subject", name="uq_group_setup_alerts_kind_subject"),
        Index("ix_group_setup_alerts_group", "group_id"),
        {"extend_existing": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # bot_missing | posts_failing | no_channel | install_no_group
    kind = Column(String(24), nullable=False)
    # "g<group_id>" or "s<guild_id>" (a server with no group yet).
    subject = Column(String(40), nullable=False)
    group_id = Column(Integer, nullable=True)
    guild_id = Column(String(32), nullable=True)
    # open | baseline | resolved
    state = Column(String(12), nullable=False, default="open")
    detail = Column(Text, nullable=True)
    # Consecutive checks the condition held (bot_missing waits for 2).
    strikes = Column(Integer, nullable=False, default=0)
    first_seen_at = Column(DateTime, nullable=False, default=datetime.now)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.now)
    send_count = Column(Integer, nullable=False, default=0)
    last_sent_at = Column(DateTime, nullable=True)
    # Who got the last message, and whether it landed.
    recipient_discord_id = Column(String(35), nullable=True)
    recipient_role = Column(String(12), nullable=True)  # owner | admin | guild_owner
    delivered = Column(Boolean, nullable=True)
    last_error = Column(String(255), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
