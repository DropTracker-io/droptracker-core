"""Read-side helpers for per-(drop, group) manual-submission exclusions.

``drop_group_moderation`` rows (db/models/drop_moderation.py) record drops a
group's ``manual_submission_policy`` withheld from THAT group. The intake path
already filters its Redis increments, but every job that RE-DERIVES group
aggregates from the ``drops`` table must apply the same exclusions or the
excluded drops leak back in:

- lootboard image generation (lootboard/generator.py)
- the force-update rebuild (services/redis_updates.py)
- scripts/reconcile_period_leaderboards.py

Global (non-group) aggregations are never filtered — exclusions are strictly
per-group. The moderation table only ever holds rows for manual submissions,
so these queries stay tiny.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, Tuple

# Match the retention windows the intake path applies (redis_updates.py).
_WEEKLY_TTL = 400 * 24 * 3600   # ~13 months
_DAILY_TTL = 90 * 24 * 3600     # 90 days
_DEFAULT_MIN_VALUE = 2_500_000  # matches drop_processor's fallback


class ModerationError(Exception):
    """Raised by approve/reject; carries an HTTP-ish (status, title, detail)."""

    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


def excluded_drop_ids_for_group(session, group_id: int) -> Set[int]:
    """drop_ids that must NOT count for ``group_id`` (any excluding status)."""
    from db.models import DropGroupModeration, EXCLUDING_STATUSES

    rows = (
        session.query(DropGroupModeration.drop_id)
        .filter(
            DropGroupModeration.group_id == group_id,
            DropGroupModeration.status.in_(EXCLUDING_STATUSES),
        )
        .all()
    )
    return {drop_id for (drop_id,) in rows}


def player_exclusion_totals(
    session, player_id: int
) -> Tuple[Dict[Tuple[int, int], int], Dict[Tuple[int, str], int]]:
    """Per-group totals to SUBTRACT when rebuilding one player's group boards.

    Returns ``(monthly, daily)``:
    - monthly: ``(group_id, partition)`` -> excluded GP total
    - daily:   ``(group_id, 'YYYYMMDD')`` -> excluded GP total
    """
    from db.models import Drop, DropGroupModeration, EXCLUDING_STATUSES

    rows = (
        session.query(
            DropGroupModeration.group_id,
            Drop.partition,
            Drop.date_added,
            Drop.value,
            Drop.quantity,
        )
        .join(Drop, Drop.drop_id == DropGroupModeration.drop_id)
        .filter(
            Drop.player_id == player_id,
            DropGroupModeration.status.in_(EXCLUDING_STATUSES),
            Drop.hidden != True,  # noqa: E712 — hidden drops are already excluded everywhere
        )
        .all()
    )
    monthly: Dict[Tuple[int, int], int] = defaultdict(int)
    daily: Dict[Tuple[int, str], int] = defaultdict(int)
    for group_id, partition, date_added, value, quantity in rows:
        total = int(value or 0) * int(quantity or 0)
        monthly[(group_id, partition)] += total
        if date_added is not None:
            daily[(group_id, date_added.strftime("%Y%m%d"))] += total
    return dict(monthly), dict(daily)


def group_excluded_drops(session, group_id: int, player_ids, partition=None) -> list:
    """Excluded drops of ``group_id`` among ``player_ids``, as plain dicts
    ``{drop_id, player_id, item_id, quantity, total_value, per_item_value}`` —
    the shape the lootboard generator needs to back excluded manual drops out
    of its per-item / per-player / total aggregates.

    ``partition``: a monthly partition (``202607`` / ``"202607"``) filters by
    ``Drop.partition``; a dashed day string (``"2026-07-11"``) filters by the
    drop's calendar day; None applies no time filter.
    """
    from sqlalchemy import func as sa_func

    from db.models import Drop, DropGroupModeration, EXCLUDING_STATUSES

    if not player_ids:
        return []
    q = (
        session.query(
            Drop.drop_id, Drop.player_id, Drop.item_id, Drop.quantity, Drop.value
        )
        .join(DropGroupModeration, DropGroupModeration.drop_id == Drop.drop_id)
        .filter(
            DropGroupModeration.group_id == group_id,
            DropGroupModeration.status.in_(EXCLUDING_STATUSES),
            Drop.player_id.in_(list(player_ids)),
            Drop.hidden != True,  # noqa: E712
        )
    )
    if partition is not None:
        p = str(partition)
        if "-" in p:
            q = q.filter(sa_func.date(Drop.date_added) == p)
        else:
            try:
                q = q.filter(Drop.partition == int(p[:6]))
            except ValueError:
                pass
    out = []
    for drop_id, player_id, item_id, quantity, value in q.all():
        quantity = int(quantity or 0)
        value = int(value or 0)
        out.append({
            "drop_id": drop_id,
            "player_id": player_id,
            "item_id": item_id,
            "quantity": quantity,
            "total_value": value * quantity,
            "per_item_value": value,
        })
    return out


def group_player_exclusion_totals_by_token(
    session, tokens_by_kind: dict
) -> Dict[Tuple[str, int, int], int]:
    """Bulk variant for the reconcile scripts.

    ``tokens_by_kind``: {"month": {202607, ...}, "day": {"20260711", ...},
    "week": {"2026-W28", ...}} — only kinds present are computed.
    Returns ``(token, group_id, player_id) -> excluded GP total`` where
    ``token`` is the string form used in the leaderboard key.
    """
    from db.models import Drop, DropGroupModeration, EXCLUDING_STATUSES
    from utils.partitions import week_token

    rows = (
        session.query(
            DropGroupModeration.group_id,
            Drop.player_id,
            Drop.partition,
            Drop.date_added,
            Drop.value,
            Drop.quantity,
        )
        .join(Drop, Drop.drop_id == DropGroupModeration.drop_id)
        .filter(
            DropGroupModeration.status.in_(EXCLUDING_STATUSES),
            Drop.hidden != True,  # noqa: E712
        )
        .all()
    )
    months = {str(m) for m in tokens_by_kind.get("month", ())}
    days = set(tokens_by_kind.get("day", ()))
    weeks = set(tokens_by_kind.get("week", ()))
    out: Dict[Tuple[str, int, int], int] = defaultdict(int)
    for group_id, player_id, partition, date_added, value, quantity in rows:
        total = int(value or 0) * int(quantity or 0)
        if months and str(partition) in months:
            out[(str(partition), group_id, player_id)] += total
        if date_added is not None:
            if days:
                day = date_added.strftime("%Y%m%d")
                if day in days:
                    out[(day, group_id, player_id)] += total
            if weeks:
                wk = week_token(date_added)
                if wk in weeks:
                    out[(wk, group_id, player_id)] += total
    return dict(out)


# ── Phase 2: review queue (approve / reject a pending manual drop) ────────────

def _credit_group_boards(player_id: int, group_id: int, value: int, drop_dt: datetime) -> None:
    """ZINCRBY a single (player, value) onto ``group_id``'s monthly / daily /
    weekly / all-time boards, keyed to the drop's own date — the mirror of the
    per-group increments the intake path skipped while the drop was withheld.
    Global boards are never touched (they always counted the drop). Daily/
    weekly are only credited inside their retention window (an ancient
    approval must not resurrect an expired board)."""
    from utils.redis import redis_client
    from utils.partitions import week_token, day_token, month_token, ALL

    if value <= 0:
        return
    conn = getattr(redis_client, "client", None)
    if conn is None:
        return
    now = datetime.now()
    # (token, ttl or None). Monthly + all-time always; day/week within window.
    boards = [(month_token(drop_dt), None), (ALL, None)]
    if drop_dt >= now - timedelta(seconds=_DAILY_TTL):
        boards.append((day_token(drop_dt), _DAILY_TTL))
    if drop_dt >= now - timedelta(seconds=_WEEKLY_TTL):
        boards.append((week_token(drop_dt), _WEEKLY_TTL))

    pipe = conn.pipeline(transaction=True)
    for token, ttl in boards:
        key = f"leaderboard:{token}:group:{group_id}"
        pipe.zincrby(key, value, player_id)
        if ttl:
            pipe.expire(key, ttl)
    pipe.execute()


def _enqueue_approved_drop_notification(session, drop, group_id: int) -> bool:
    """Release the group notification the intake path withheld — subject to the
    SAME gates intake applies (minimum_value_to_notify + screenshot
    requirement), so approving doesn't announce a drop the group would never
    have notified. Enqueues a NotificationQueue row (the core bot's
    notification_service renders + sends it). Returns True if enqueued."""
    from db.models import GroupConfiguration, ItemList, NotificationQueue, NpcList, Player
    from utils import group_config as gc

    value = int(drop.value or 0)
    quantity = int(drop.quantity or 0)
    total_value = value * quantity

    cfg = gc.get_bulk(session, [group_id], ["minimum_value_to_notify", "only_send_messages_with_images"])
    raw_min = cfg.get((group_id, "minimum_value_to_notify"))
    try:
        min_value = int(raw_min) if raw_min is not None else _DEFAULT_MIN_VALUE
    except (TypeError, ValueError):
        min_value = _DEFAULT_MIN_VALUE
    if total_value < min_value:
        return False
    if gc.is_truthy(cfg.get((group_id, "only_send_messages_with_images"))) and not drop.image_url:
        return False

    item = session.query(ItemList).filter(ItemList.item_id == drop.item_id).first()
    npc = session.query(NpcList).filter(NpcList.npc_id == drop.npc_id).first()
    player = session.query(Player).filter(Player.player_id == drop.player_id).first()
    data = {
        "drop_id": drop.drop_id,
        "item_name": item.item_name if item else "Unknown item",
        "npc_name": npc.npc_name if npc else "Unknown",
        "value": value,
        "quantity": quantity,
        "total_value": total_value,
        "player_name": player.player_name if player else "Unknown",
        "player_id": drop.player_id,
        "image_url": drop.image_url,
        "kill_count": None,
        "world_type": "main",
        "approved_manual": True,
    }
    session.add(NotificationQueue(
        notification_type="drop",
        player_id=drop.player_id,
        data=json.dumps(data),
        group_id=group_id,
        status="pending",
    ))
    return True


def _load_pending_row(session, drop_id: int, group_id: int):
    from db.models import DropGroupModeration

    return (
        session.query(DropGroupModeration)
        .filter(
            DropGroupModeration.drop_id == drop_id,
            DropGroupModeration.group_id == group_id,
        )
        .first()
    )


def approve_drop_for_group(session, drop_id: int, group_id: int,
                           reviewer_user_id: Optional[int] = None) -> dict:
    """Approve a pending manual drop for one group: flip the moderation row to
    ``approved`` (so read-path rebuilds now include it), retro-apply the
    group's leaderboard credit, and release the (gated) group notification.
    The caller owns the commit. Returns a small result dict; raises
    ``ModerationError`` on a bad state."""
    from db.models import Drop

    row = _load_pending_row(session, drop_id, group_id)
    if row is None:
        raise ModerationError(404, "Not found", "That submission has no review row for this group.")
    if row.status != "pending":
        raise ModerationError(409, "Already reviewed", f"That submission is already {row.status}.")
    drop = session.query(Drop).filter(Drop.drop_id == drop_id).first()
    if drop is None:
        raise ModerationError(404, "Drop not found", f"Drop {drop_id} no longer exists.")

    row.status = "approved"
    row.reviewed_by_user_id = reviewer_user_id
    row.reviewed_at = datetime.now()
    session.flush()

    total_value = int(drop.value or 0) * int(drop.quantity or 0)
    drop_dt = drop.date_added or datetime.now()
    try:
        _credit_group_boards(drop.player_id, group_id, total_value, drop_dt)
    except Exception as e:
        print(f"[ManualReview] Board credit failed for drop {drop_id} group {group_id}: {e}")
    notified = False
    try:
        notified = _enqueue_approved_drop_notification(session, drop, group_id)
    except Exception as e:
        print(f"[ManualReview] Notification enqueue failed for drop {drop_id} group {group_id}: {e}")
    return {"drop_id": drop_id, "group_id": group_id, "status": "approved",
            "credited": total_value, "notified": notified}


def reject_drop_for_group(session, drop_id: int, group_id: int,
                          reviewer_user_id: Optional[int] = None) -> dict:
    """Reject a pending manual drop: it stays withheld from the group (rejected
    is an excluding status) but never counts. The drop still exists globally /
    for other groups. Caller owns the commit."""
    row = _load_pending_row(session, drop_id, group_id)
    if row is None:
        raise ModerationError(404, "Not found", "That submission has no review row for this group.")
    if row.status != "pending":
        raise ModerationError(409, "Already reviewed", f"That submission is already {row.status}.")
    row.status = "rejected"
    row.reviewed_by_user_id = reviewer_user_id
    row.reviewed_at = datetime.now()
    session.flush()
    return {"drop_id": drop_id, "group_id": group_id, "status": "rejected"}
