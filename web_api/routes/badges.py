"""Badge endpoints (public reads).

  GET /api/v1/badges                        - active badge catalog
  GET /api/v1/players/{id}/badges           - a player's award history

Admin CRUD/award/revoke live in routes/admin.py (superadmin + audit log);
the compact per-row badges on leaderboards are embedded by
routes/leaderboards.py.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify

from db import Badge, PlayerBadge
from web_api.common import db_session, with_cache_headers

badges_bp = Blueprint("v1_badges", __name__)

IMG_BASE = "https://www.droptracker.io/img"


def serialize_definition(b: Badge) -> dict:
    return {
        "key": b.key,
        "name": b.name,
        "description": b.description,
        "icon_url": b.icon_url,
        "icon_emoji": b.icon_emoji,
        "tone": b.tone,
        "semantic": b.semantic,
    }


def serialize_award(award: PlayerBadge, badge: Badge) -> dict:
    context = None
    if award.context:
        try:
            context = json.loads(award.context)
        except (TypeError, ValueError):
            context = None
    icon_url = badge.icon_url
    # NPC-scoped awards fall back to the NPC's icon (same convention as PB
    # submissions in routes/profiles.py).
    if not icon_url and isinstance(context, dict) and context.get("npc_id"):
        icon_url = f"{IMG_BASE}/npcdb/{context['npc_id']}.png"
    out = {
        "id": int(award.id),
        "key": badge.key,
        "name": badge.name,
        "description": badge.description,
        "icon_url": icon_url,
        "icon_emoji": badge.icon_emoji,
        "tone": badge.tone,
        "semantic": badge.semantic,
        "status": award.status,
        "awarded_at_ts": int(award.awarded_at.timestamp()) if award.awarded_at else None,
        "lost_at_ts": int(award.lost_at.timestamp()) if award.lost_at else None,
        "context": context,
    }
    return out


def player_awards(s, player_id: int, include_revoked: bool = False) -> list[dict]:
    """Awards for a player's profile, newest first. Lost held badges are kept
    (rendered dimmed as history); revoked awards are hidden by default."""
    statuses = ("active", "lost", "revoked") if include_revoked else ("active", "lost")
    rows = (
        s.query(PlayerBadge, Badge)
        .join(Badge, Badge.badge_id == PlayerBadge.badge_id)
        .filter(
            PlayerBadge.player_id == player_id,
            PlayerBadge.status.in_(statuses),
            Badge.active == True,  # noqa: E712
        )
        .order_by(PlayerBadge.awarded_at.desc())
        .all()
    )
    return [serialize_award(a, b) for a, b in rows]


@badges_bp.get("/badges")
async def badge_catalog():
    def _load():
        with db_session() as s:
            rows = (
                s.query(Badge)
                .filter(Badge.active == True)  # noqa: E712
                .order_by(Badge.badge_id.asc())
                .all()
            )
            return [serialize_definition(b) for b in rows]

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=300)


@badges_bp.get("/players/<int:player_id>/badges")
async def player_badges(player_id: int):
    def _load():
        with db_session() as s:
            return player_awards(s, player_id)

    payload = await asyncio.to_thread(_load)
    return with_cache_headers(jsonify(payload), max_age=30)
