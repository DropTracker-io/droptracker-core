"""Clan stats and recap designs (web154a, suggestion #224).

Public:
  GET  /api/v1/groups/{id}/stats?period=YYYY-MM|YYYY|all   the clan stats payload
  GET  /api/v1/groups/{id}/recap-design?period=YYYY-MM     the card's effective design

Group admins:
  GET    /api/v1/groups/{id}/recap-design/admin?period=     designer state
  PUT    /api/v1/groups/{id}/recap-design                    save {period, design}
  DELETE /api/v1/groups/{id}/recap-design?period=            reset one level
  POST   /api/v1/groups/{id}/recaps/{period}/approve         {approved: bool}
  POST   /api/v1/groups/{id}/recaps/{period}/refresh-post    re-render + edit the post
  POST   /api/v1/groups/{id}/recaps/{period}/regenerate      rebuild the snapshot

The compute lives in ``services.group_stats``; the design rules in
``services.recap_design``. ``services`` is a stub package under the unit
tests, so every service import here is inside a handler.
"""
from __future__ import annotations

import asyncio
import json

from quart import Blueprint, jsonify, request
from sqlalchemy import text

from db import AuditLog
from web_api.common import _rc, abort_problem, db_session, private_no_store, with_cache_headers
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

group_stats_bp = Blueprint("v1_group_stats", __name__)

# Seconds a browser/CDN may reuse a stats response. The payload itself is
# cached server-side (10 min for a month in progress, a day once closed).
_STATS_CACHE_SECONDS = 60
# The design endpoint is read by the card page (ISR) and the capture route; a
# minute is the longest an admin should wait to see an edit on the page.
_DESIGN_CACHE_SECONDS = 30
# Throttles on the two admin actions that cost a render or a recompute.
_REFRESH_COOLDOWN = 60
_REGENERATE_COOLDOWN = 600


def _assert_admin(user_id: int, group_id: int) -> None:
    with db_session() as s:
        user = load_user(s, user_id)
        assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)


def _period_arg(required: bool = False) -> str:
    period = (request.args.get("period") or "").strip()
    if required and not period:
        abort_problem(422, "period is required")
    return period


def _throttle(key: str, seconds: int) -> bool:
    """True when the caller may proceed; sets the cooldown."""
    conn = _rc()
    if conn is None:
        return True
    try:
        return bool(conn.set(key, "1", nx=True, ex=seconds))
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@group_stats_bp.get("/groups/<int:group_id>/stats")
async def get_group_stats(group_id: int):
    from services.group_stats import get_group_stats as compute

    period = _period_arg() or None
    try:
        payload = await compute(group_id, period)
    except Exception:
        from db.app_logger import AppLogger

        AppLogger().log(
            log_type="error",
            data=f"clan stats failed for group {group_id} {period}",
            app_name="web_api",
            description="get_group_stats",
        )
        return abort_problem(500, "Stats could not be built")
    if payload is None:
        return abort_problem(404, "No stats for that group or period")
    return with_cache_headers(jsonify(payload), _STATS_CACHE_SECONDS)


# --------------------------------------------------------------------------- #
# Designs
# --------------------------------------------------------------------------- #
def _resolve(group_id: int, period: str) -> dict:
    from services.recap_design import resolve

    with db_session() as s:
        return resolve(s, group_id, period or None)


@group_stats_bp.get("/groups/<int:group_id>/recap-design")
async def get_recap_design(group_id: int):
    period = _period_arg()
    resolved = await asyncio.to_thread(_resolve, group_id, period)
    return with_cache_headers(
        jsonify({"design": resolved["design"], "source": resolved["source"],
                 "updated_at": resolved["updated_at"]}),
        _DESIGN_CACHE_SECONDS,
    )


def _admin_state(group_id: int, period: str) -> dict:
    """Everything the designer needs in one read."""
    from services.recap import _player_names, _visible_group_player_ids, load_snapshot
    from services.recap_delivery import (
        CFG_CHANNEL, CFG_ENABLED, CFG_HOUR, CFG_LOOTBOARD_CHANNEL, CFG_REVIEW,
        CFG_TIMEZONE, DEFAULT_POST_HOUR, due_at_utc, review_mode,
    )
    from services.recap_design import resolve
    from db.models.recap import DELIVERY_CHANNEL, DELIVERY_SENT, RecapDelivery, SCOPE_GROUP
    from utils import group_config as gc

    with db_session() as s:
        resolved = resolve(s, group_id, period or None)
        cfg = gc.get_bulk(s, [group_id], [CFG_ENABLED, CFG_REVIEW, CFG_HOUR, CFG_TIMEZONE,
                                          CFG_CHANNEL, CFG_LOOTBOARD_CHANNEL])
        value = lambda key: cfg.get((group_id, key))  # noqa: E731
        raw_hour = value(CFG_HOUR)
        try:
            hour = DEFAULT_POST_HOUR if raw_hour in (None, "") else int(raw_hour)
        except (TypeError, ValueError):
            hour = DEFAULT_POST_HOUR
        post = None
        snapshot = None
        if period:
            row = (
                s.query(RecapDelivery)
                .filter(RecapDelivery.scope == SCOPE_GROUP,
                        RecapDelivery.subject_id == group_id,
                        RecapDelivery.period == period,
                        RecapDelivery.kind == DELIVERY_CHANNEL,
                        RecapDelivery.is_test == 0)
                .first()
            )
            if row is not None:
                post = {
                    "status": row.status,
                    "channel_id": row.target_id,
                    "message_id": row.message_id,
                    "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                    "posted": row.status == DELIVERY_SENT and bool(row.message_id),
                }
            snapshot = load_snapshot(s, SCOPE_GROUP, group_id, period)
        try:
            due = due_at_utc(period, value(CFG_TIMEZONE), hour).isoformat() if period else None
        except Exception:
            due = None
        guild = s.execute(
            text("SELECT guild_id FROM groups WHERE group_id = :gid"),
            {"gid": group_id},
        ).first()
        roster = _visible_group_player_ids(s, group_id)
        names = _player_names(s, roster)

    return {
        "period": period or "",
        "design": resolved["design"],
        "source": resolved["source"],
        "has_month": resolved["has_month"],
        "has_default": resolved["has_default"],
        "updated_at": resolved["updated_at"],
        "approved_at": resolved["approved_at"],
        "review_mode": review_mode(value(CFG_REVIEW)),
        "recaps_enabled": gc.is_truthy(value(CFG_ENABLED)),
        "channel_id": (value(CFG_CHANNEL) or value(CFG_LOOTBOARD_CHANNEL) or None),
        "guild_id": str(guild[0]) if guild and guild[0] else None,
        "post_due_at": due,
        "post": post,
        "has_snapshot": snapshot is not None,
        "snapshot_has_standouts": bool(snapshot and snapshot.get("standouts")),
        "members": sorted(
            ({"player_id": pid, "name": names.get(pid)} for pid in roster if names.get(pid)),
            key=lambda m: (m["name"] or "").lower(),
        ),
    }


@group_stats_bp.get("/groups/<int:group_id>/recap-design/admin")
async def get_recap_design_admin(group_id: int):
    user_id = current_user_id()
    await asyncio.to_thread(_assert_admin, user_id, group_id)
    period = _period_arg()
    from services.recap_design import is_valid_period

    if not is_valid_period(period):
        return abort_problem(422, "Invalid period")
    state = await asyncio.to_thread(_admin_state, group_id, period)
    return private_no_store(jsonify(state))


def _save(user_id: int, group_id: int, period: str, design) -> dict:
    from services.recap_design import is_valid_period, save

    if not is_valid_period(period):
        abort_problem(422, "Invalid period")
    with db_session() as s:
        before = s.execute(
            text(
                "SELECT design FROM recap_designs WHERE group_id = :gid AND period = :p"
            ),
            {"gid": group_id, "p": period},
        ).first()
        resolved = save(s, group_id, period, design, user_id)
        s.add(AuditLog(
            actor_user_id=user_id, group_id=group_id, action="recap_design.update",
            target=f"recap_designs.{period or 'default'}",
            before=(before[0] if before else None),
            after=json.dumps(resolved["design"], separators=(",", ":")),
        ))
        s.commit()
        return resolved


@group_stats_bp.put("/groups/<int:group_id>/recap-design")
async def put_recap_design(group_id: int):
    user_id = current_user_id()
    await asyncio.to_thread(_assert_admin, user_id, group_id)
    body = await json_body()
    period = str(body.get("period") or "")
    design = body.get("design")
    if not isinstance(design, dict):
        return abort_problem(422, "design must be an object")
    resolved = await asyncio.to_thread(_save, user_id, group_id, period, design)
    return private_no_store(jsonify({
        "design": resolved["design"], "source": resolved["source"],
        "updated_at": resolved["updated_at"],
    }))


def _reset(user_id: int, group_id: int, period: str) -> dict:
    from services.recap_design import delete, is_valid_period, resolve

    if not is_valid_period(period):
        abort_problem(422, "Invalid period")
    with db_session() as s:
        delete(s, group_id, period)
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id,
                       action="recap_design.reset",
                       target=f"recap_designs.{period or 'default'}"))
        s.commit()
        return resolve(s, group_id, period or None)


@group_stats_bp.delete("/groups/<int:group_id>/recap-design")
async def delete_recap_design(group_id: int):
    user_id = current_user_id()
    await asyncio.to_thread(_assert_admin, user_id, group_id)
    resolved = await asyncio.to_thread(_reset, user_id, group_id, _period_arg())
    return private_no_store(jsonify({
        "design": resolved["design"], "source": resolved["source"],
        "updated_at": resolved["updated_at"],
    }))


# --------------------------------------------------------------------------- #
# Review actions
# --------------------------------------------------------------------------- #
def _approve(user_id: int, group_id: int, period: str, approved: bool) -> None:
    from services.recap_design import is_valid_period, set_approval

    if not period or not is_valid_period(period):
        abort_problem(422, "Invalid period")
    with db_session() as s:
        set_approval(s, group_id, period, user_id, approved)
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id,
                       action="recap.approve" if approved else "recap.unapprove",
                       target=f"recap_designs.{period}"))
        s.commit()


@group_stats_bp.post("/groups/<int:group_id>/recaps/<period>/approve")
async def approve_recap(group_id: int, period: str):
    user_id = current_user_id()
    await asyncio.to_thread(_assert_admin, user_id, group_id)
    body = await json_body(required=False)
    approved = bool(body.get("approved", True)) if isinstance(body, dict) else True
    await asyncio.to_thread(_approve, user_id, group_id, period, approved)
    return private_no_store(jsonify({"ok": True, "approved": approved}))


def _enqueue_refresh(user_id: int, group_id: int, period: str) -> dict:
    from services import discord_outbox
    from db.models.recap import DELIVERY_CHANNEL, DELIVERY_SENT, RecapDelivery, SCOPE_GROUP

    with db_session() as s:
        row = (
            s.query(RecapDelivery)
            .filter(RecapDelivery.scope == SCOPE_GROUP,
                    RecapDelivery.subject_id == group_id,
                    RecapDelivery.period == period,
                    RecapDelivery.kind == DELIVERY_CHANNEL,
                    RecapDelivery.is_test == 0,
                    RecapDelivery.status == DELIVERY_SENT)
            .first()
        )
        if row is None or not row.message_id:
            abort_problem(409, "Not posted yet",
                          "This recap hasn't been posted to Discord, so there is nothing to update.")
        # Web API never talks to Discord itself: the core bot drains this.
        discord_outbox.enqueue(
            s, channel_id=str(row.target_id), kind="recap_refresh",
            embed={"group_id": group_id, "period": period},
            ref_type="recap", ref_id=group_id, actor_user_id=user_id, commit=False,
        )
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id,
                       action="recap.refresh_post", target=f"recap_deliveries.{period}"))
        s.commit()
    return {"ok": True, "queued": True}


@group_stats_bp.post("/groups/<int:group_id>/recaps/<period>/refresh-post")
async def refresh_recap_post(group_id: int, period: str):
    user_id = current_user_id()
    await asyncio.to_thread(_assert_admin, user_id, group_id)
    if not _throttle(f"recap:refresh:{group_id}:{period}", _REFRESH_COOLDOWN):
        return abort_problem(429, "Slow down", "The post was updated less than a minute ago.")
    result = await asyncio.to_thread(_enqueue_refresh, user_id, group_id, period)
    return private_no_store(jsonify(result))


def _regenerate(user_id: int, group_id: int, period: str) -> dict:
    from services.recap import compute_group_month, is_month_period, period_closed, save_snapshot
    from db.models.recap import SCOPE_GROUP

    if not is_month_period(period) or not period_closed(period):
        abort_problem(422, "Only a finished month can be rebuilt")
    with db_session() as s:
        payload = compute_group_month(s, group_id, period)
        if payload is None:
            abort_problem(404, "No card for that month",
                          "The clan was below the activity floor that month.")
        save_snapshot(s, SCOPE_GROUP, group_id, period, payload)
        s.add(AuditLog(actor_user_id=user_id, group_id=group_id,
                       action="recap.regenerate", target=f"recap_snapshots.{period}"))
        s.commit()
    return {"ok": True}


@group_stats_bp.post("/groups/<int:group_id>/recaps/<period>/regenerate")
async def regenerate_recap(group_id: int, period: str):
    """Rebuild a closed month's card from current data — so a card generated
    before standouts existed (or before the clan's WOM data was harvested)
    gains them. The posted message is not touched; refresh-post does that."""
    user_id = current_user_id()
    await asyncio.to_thread(_assert_admin, user_id, group_id)
    if not _throttle(f"recap:regen:{group_id}:{period}", _REGENERATE_COOLDOWN):
        return abort_problem(429, "Slow down", "This card was rebuilt in the last 10 minutes.")
    try:
        from services.recap_ehb import ensure_group_month

        await ensure_group_month(group_id, period)
    except Exception:
        pass
    result = await asyncio.to_thread(_regenerate, user_id, group_id, period)
    try:
        from services.group_stats import invalidate

        invalidate(group_id)
    except Exception:
        pass
    return private_no_store(jsonify(result))
