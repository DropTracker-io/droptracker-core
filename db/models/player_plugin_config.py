"""The latest DropTracker plugin settings each player's client reported.

The plugin sends a ``config_snapshot`` submission after login and whenever
its settings change (plugin 6.0.16+, ``ConfigSnapshotHandler``), so group
leaders and staff can see how a player's plugin is set up when helping them
debug it. One row per player, overwritten in place; the snapshot before the
latest is kept alongside so a viewer can show what just changed.

Self-reported: the intake identifies the player by account hash, like every
other plugin submission. Fine for debug data, never an authority.
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func

from .base import Base


class PlayerPluginConfig(Base):
    __tablename__ = "player_plugin_config"
    __table_args__ = ({"extend_existing": True},)

    player_id = Column(Integer, ForeignKey("players.player_id"), primary_key=True)
    # The snapshot as the plugin sent it: {"v", "settings": {section: {key: value}},
    # "customized": [keys], "env": {...}, "hash"}.
    config_json = Column(Text, nullable=False)
    config_hash = Column(String(64), nullable=True)
    plugin_version = Column(String(32), nullable=True)
    runelite_version = Column(String(32), nullable=True)
    # True when it came through the API, False through a Discord webhook.
    used_api = Column(Boolean, nullable=True)
    captured_at = Column(DateTime, nullable=False)
    previous_config_json = Column(Text, nullable=True)
    previous_captured_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
