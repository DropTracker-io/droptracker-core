"""Discord delivery relay ORM models (web153a).

A *relay integration* lets a group's own Discord bot deliver DropTracker's
messages in the group's server instead of the DropTracker bot. Layouts,
toggles and channel choices stay on the site; the integration only changes
WHO posts. See ``services/discord_relay_core.py`` for the delivery protocol.

Two tables:
    DiscordRelayIntegration - one customer bot: owning group, the bound
                              data-API key, which features it takes over.
    DiscordRelayGuild       - the guilds it owns delivery for. ``guild_id`` is
                              UNIQUE: a server has exactly one sender per
                              integration-managed feature, enforced by the DB
                              rather than by whoever edits the config last.

No Discord bot token is ever stored. The customer's bot authenticates to the
data API with an ordinary ``dtk_`` key and does every Discord call itself.
Delivery state (the outbox, acks, stats) lives in Redis (``relay:*``), so the
data API's SELECT-only DB account never needs a write grant.
"""
from sqlalchemy import (
    BigInteger,
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


class DiscordRelayIntegration(Base):
    __tablename__ = "discord_relay_integrations"
    __table_args__ = (
        Index("idx_discord_relay_group", "group_id"),
        Index("uq_discord_relay_api_key", "api_key_id", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.group_id"), nullable=False)
    label = Column(String(64), nullable=False, default="")
    #: The customer bot's Discord application id. Relayed interactions must
    #: carry it, so a key can't forward another app's clicks. Nullable until
    #: staff fill it in; interactions are refused while it is unset.
    application_id = Column(String(32), nullable=True)
    #: The data-API key the bot authenticates with (one key per integration).
    api_key_id = Column(BigInteger, ForeignKey("api_keys.id"), nullable=True)
    #: Which DropTracker features it delivers — see
    #: ``discord_relay_core.FEATURES``. A JSON list, e.g. ``["events"]``.
    features = Column(JSON, nullable=False)
    #: 'active' delivers through the relay; 'paused' hands every message back
    #: to the DropTracker bot (the instant fallback).
    status = Column(String(16), nullable=False, default="paused")
    #: How long an undelivered *new* message waits for the bot before it is
    #: dropped as stale. Live boards are edits and always catch up.
    hold_minutes = Column(Integer, nullable=False, default=360)
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(),
                        onupdate=func.now())


class DiscordRelayGuild(Base):
    __tablename__ = "discord_relay_guilds"
    __table_args__ = (
        Index("idx_discord_relay_guild_integration", "integration_id"),
        Index("uq_discord_relay_guild", "guild_id", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    integration_id = Column(Integer, ForeignKey("discord_relay_integrations.id",
                                                ondelete="CASCADE"),
                            nullable=False)
    guild_id = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
