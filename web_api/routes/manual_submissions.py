"""Manual-submission review queue (suggestion #45, Phase 2).

Under the ``confirm`` manual_submission_policy an unauthorized member's manual
drop is held ``pending`` until a group admin approves it. This surfaces the
queue and the approve/reject actions.

  GET    /api/v1/groups/{gid}/manual-submissions            -> { pending, recent }
  POST   /api/v1/groups/{gid}/manual-submissions/{drop_id}/approve
  POST   /api/v1/groups/{gid}/manual-submissions/{drop_id}/reject

Auth: group admin. Approving retro-applies the group's leaderboard credit and
releases the (gated) group notification; rejecting leaves the drop withheld
from this group only (it still counts globally / for the player's other
groups). See services/drop_moderation.py.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from db import (
    AuditLog,
    Drop,
    DropGroupModeration,
    ItemList,
    NpcList,
    Player,
)
from web_api.common import abort_problem, db_session, money, private_no_store
from web_api.deps import assert_group_admin, current_user_id, manageable_guild_ids

manual_submissions_bp = Blueprint("v1_manual_submissions", __name__)

_RECENT_LIMIT = 25


def _row_dict(mod, drop, item_name, npc_name, player_name) -> dict:
    total = int(drop.value or 0) * int(drop.quantity or 0)
    return {
        "drop_id": drop.drop_id,
        "status": mod.status,
        "player_id": drop.player_id,
        "player_name": player_name,
        "item_id": drop.item_id,
        "item_name": item_name,
        "npc_name": npc_name,
        "quantity": int(drop.quantity or 0),
        "value": money(total),
        "image_url": drop.image_url or None,
        "submitted_ts": int(drop.date_added.timestamp()) if drop.date_added else None,
        "reviewed_ts": int(mod.reviewed_at.timestamp()) if mod.reviewed_at else None,
        "reason": mod.reason,
    }


def _query(s, group_id: int, statuses, limit=None):
    q = (
        s.query(DropGroupModeration, Drop, ItemList.item_name, NpcList.npc_name, Player.player_name)
        .join(Drop, Drop.drop_id == DropGroupModeration.drop_id)
        .outerjoin(ItemList, ItemList.item_id == Drop.item_id)
        .outerjoin(NpcList, NpcList.npc_id == Drop.npc_id)
        .outerjoin(Player, Player.player_id == Drop.player_id)
        .filter(
            DropGroupModeration.group_id == group_id,
            DropGroupModeration.status.in_(statuses),
        )
        .order_by(DropGroupModeration.id.desc())
    )
    if limit:
        q = q.limit(limit)
    return [
        _row_dict(mod, drop, item_name, npc_name, player_name)
        for mod, drop, item_name, npc_name, player_name in q.all()
    ]


@manual_submissions_bp.get("/groups/<int:group_id>/manual-submissions")
async def list_manual_submissions(group_id: int):
    """Pending manual drops for the group's review queue, plus the last few
    reviewed ones for context."""
    user_id = current_user_id()

    # Resolved outside the session block, the way group_admin.py does it: the
    # MANAGE_GUILD-derived role source is what admits a Discord-side admin who
    # has no explicit GroupAdmin row, and omitting it 403s exactly those people
    # out of the queue they are supposed to moderate.
    manage_ids = manageable_guild_ids(user_id)

    def _load():
        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manage_ids)
            pending = _query(s, group_id, ("pending",))
            recent = _query(s, group_id, ("approved", "rejected"), limit=_RECENT_LIMIT)
            return {"pending": pending, "recent": recent, "pending_count": len(pending)}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


def _review(group_id: int, drop_id: int, approve: bool):
    from services.drop_moderation import (
        ModerationError,
        approve_drop_for_group,
        reject_drop_for_group,
    )

    user_id = current_user_id()
    manage_ids = manageable_guild_ids(user_id)
    with db_session() as s:
        assert_group_admin(s, user_id, group_id, manage_ids)
        try:
            if approve:
                result = approve_drop_for_group(s, drop_id, group_id, reviewer_user_id=user_id)
            else:
                result = reject_drop_for_group(s, drop_id, group_id, reviewer_user_id=user_id)
        except ModerationError as e:
            abort_problem(e.status, e.title, e.detail)
        s.add(AuditLog(
            actor_user_id=user_id,
            group_id=group_id,
            action=f"manual_submission.{'approve' if approve else 'reject'}",
            target=f"drops.{drop_id}.group.{group_id}",
            before="pending",
            after=result["status"],
        ))
        s.commit()
        return result


@manual_submissions_bp.post("/groups/<int:group_id>/manual-submissions/<int:drop_id>/approve")
async def approve_manual_submission(group_id: int, drop_id: int):
    result = await asyncio.to_thread(_review, group_id, drop_id, True)
    return private_no_store(jsonify(result))


@manual_submissions_bp.post("/groups/<int:group_id>/manual-submissions/<int:drop_id>/reject")
async def reject_manual_submission(group_id: int, drop_id: int):
    result = await asyncio.to_thread(_review, group_id, drop_id, False)
    return private_no_store(jsonify(result))
