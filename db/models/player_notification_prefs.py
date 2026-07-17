from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey
from datetime import datetime

from .base import Base


class PlayerNotificationPrefs(Base):
    """Per-player website preferences for in-game plugin notifications
    (docs/EVENT_PLUGIN_NOTIFICATIONS_PLAN.md). ``prefs`` is a JSON object
    mapping notification type → bool; absent keys (and an absent row) mean
    enabled. Only types in services/plugin_notifications.WEB_PREF_TYPES are
    meaningful here — event_task_progress is client-toggle-only by design."""
    __tablename__ = 'player_notification_prefs'
    __table_args__ = ({'extend_existing': True},)

    player_id = Column(Integer, ForeignKey('players.player_id'), primary_key=True)
    prefs = Column(Text, nullable=False, default='{}')
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
