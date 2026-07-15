"""Queue Discord notifications for Nitro server boosts.

The DM to a booster and the contributors-channel message are enqueued here and
sent by the MAIN bot (droptracker-core / ``services/notification_service.py``) —
it shares more guilds and is more recognizable than the internal webhook bot
(which merely detects the boost + reconciles credit). Mirrors
``services/contribution_notifications.py``; must stay free of discord/interactions
imports so any process can enqueue.

Two types:
  * ``nitro_boost`` — one per booster: a confirmation DM (with a clan picker for
    multi-group boosters) and, when ``announce`` is set, a one-line channel post.
  * ``nitro_boost_summary`` — ONE consolidated channel post (used by the
    retroactive backfill so it isn't spammy).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError

from db.models import NotificationQueue, Player, User
from db.models.base import Session

logger = logging.getLogger("services.nitro_notifications")

NITRO_TYPE = "nitro_boost"
NITRO_SUMMARY_TYPE = "nitro_boost_summary"


def _first_player_id(s, user_id: Optional[int]) -> int:
    """notification_queue.player_id is NOT NULL — the booster's first tracked
    player, else the raw user_id, else 0 (mirrors contribution_notifications)."""
    if user_id is not None:
        row = s.query(Player.player_id).filter(Player.user_id == user_id).first()
        if row:
            return int(row[0])
    return int(user_id or 0)


def queue_nitro_boost(discord_id, announce: bool = True, session=None) -> bool:
    """Queue a per-booster ``nitro_boost`` notification. Best-effort; never raises.

    Dedup: the ``notification_queue`` unique constraint (type, player_id,
    group_id, data) drops a duplicate enqueue for the same booster.
    """
    own = session is None
    s = session or Session()
    try:
        user = s.query(User).filter(User.discord_id == str(discord_id)).first()
        user_id = user.user_id if user else None
        payload = {"discord_id": str(discord_id), "user_id": user_id, "announce": bool(announce)}
        s.add(
            NotificationQueue(
                notification_type=NITRO_TYPE,
                player_id=_first_player_id(s, user_id),
                group_id=None,
                data=json.dumps(payload, sort_keys=True),
                status="pending",
                created_at=datetime.now(),
            )
        )
        try:
            s.commit()
        except IntegrityError:
            s.rollback()  # already queued
            return False
        return True
    except Exception:
        logger.exception("Failed to queue nitro_boost for %s", discord_id)
        try:
            s.rollback()
        except Exception:
            pass
        return False
    finally:
        if own:
            s.close()


def queue_nitro_boost_summary(entries, credited_cents: int, session=None) -> bool:
    """Queue ONE consolidated ``nitro_boost_summary`` channel post.

    ``entries`` is an iterable of ``(discord_id, group_name_or_None)``.
    """
    own = session is None
    s = session or Session()
    try:
        payload = {
            "entries": [{"discord_id": str(d), "group": g} for d, g in entries],
            "credited_cents": int(credited_cents),
        }
        # A valid player_id keeps the NOT-NULL FK happy for this player-less row.
        player_id = 0
        row = s.query(Player.player_id).first()
        if row:
            player_id = int(row[0])
        s.add(
            NotificationQueue(
                notification_type=NITRO_SUMMARY_TYPE,
                player_id=player_id,
                group_id=None,
                data=json.dumps(payload, sort_keys=True),
                status="pending",
                created_at=datetime.now(),
            )
        )
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return False
        return True
    except Exception:
        logger.exception("Failed to queue nitro_boost_summary")
        try:
            s.rollback()
        except Exception:
            pass
        return False
    finally:
        if own:
            s.close()
