"""Task 04 — player and group profiles.

GET /api/v1/players/{id}   -> PlayerProfile
GET /api/v1/groups/{id}    -> GroupProfile
GET /api/v1/groups/{id}/members?page=
"""
from __future__ import annotations

import asyncio

from sqlalchemy import or_, text
from quart import Blueprint, jsonify, request

from db import (
    Player,
    Group,
    NotifiedSubmission,
    Drop,
    ItemList,
    CollectionLogEntry,
    PersonalBestEntry,
    NpcList,
)
from web_api.common import (
    db_session,
    decode_member,
    leaderboard_key,
    money,
    parse_page,
    period_to_partition,
    player_global_rank,
    player_list_loot_sum,
    player_month_total,
    problem,
    _rc,
)
from web_api.routes.leaderboards import _compute_group_totals

profiles_bp = Blueprint("v1_profiles", __name__)

IMG_BASE = "https://www.droptracker.io/img"


def _convert_from_ms(ms: int) -> str:
    try:
        ms = int(ms)
    except Exception:
        return ""
    hours = ms // 3_600_000
    ms %= 3_600_000
    minutes = ms // 60_000
    ms %= 60_000
    seconds = ms // 1000
    ms %= 1000
    tenths = ms // 100
    if hours > 0:
        return f"{hours}:{minutes:02}:{seconds:02}.{tenths}"
    return f"{minutes}:{seconds:02}.{tenths}"


def _build_submissions(rows, s, partition):
    """Map NotifiedSubmission rows to the contract SubmissionSchema list."""
    out = []
    seen = set()
    for sub in rows:
        try:
            ts = int(sub.date_added.timestamp()) if sub.date_added else 0
        except Exception:
            ts = 0

        if getattr(sub, "drop_id", None):
            key = ("drop", sub.drop_id)
            if key in seen:
                continue
            seen.add(key)
            drop = s.query(Drop).filter(Drop.drop_id == sub.drop_id).first()
            if not drop:
                continue
            item = s.query(ItemList).filter(ItemList.item_id == drop.item_id).first()
            label = item.item_name if item else f"Item {drop.item_id}"
            out.append({
                "id": int(sub.drop_id),
                "type": "drop",
                "label": label,
                "value": money((drop.value or 0) * (drop.quantity or 1)),
                "image_url": f"{IMG_BASE}/itemdb/{drop.item_id}.png",
                "ts": ts,
            })
        elif getattr(sub, "clog_id", None):
            key = ("clog", sub.clog_id)
            if key in seen:
                continue
            seen.add(key)
            clog = s.query(CollectionLogEntry).filter(CollectionLogEntry.log_id == sub.clog_id).first()
            if not clog:
                continue
            item = s.query(ItemList).filter(ItemList.item_id == clog.item_id).first()
            label = item.item_name if item else f"Item {clog.item_id}"
            out.append({
                "id": int(sub.clog_id),
                "type": "clog",
                "label": label,
                "image_url": f"{IMG_BASE}/itemdb/{clog.item_id}.png",
                "ts": ts,
            })
        elif getattr(sub, "pb_id", None):
            key = ("pb", sub.pb_id)
            if key in seen:
                continue
            seen.add(key)
            pb = s.query(PersonalBestEntry).filter(PersonalBestEntry.id == sub.pb_id).first()
            if not pb:
                continue
            npc = s.query(NpcList).filter(NpcList.npc_id == pb.npc_id).first()
            npc_name = npc.npc_name if npc else "Unknown"
            raw_time = 0
            if pb.personal_best and 0 < pb.personal_best < (pb.kill_time or float("inf")):
                raw_time = pb.personal_best
            elif pb.kill_time and pb.kill_time > 0:
                raw_time = pb.kill_time
            label = f"{npc_name} PB: {_convert_from_ms(raw_time)}" if raw_time else f"{npc_name} PB"
            out.append({
                "id": int(sub.pb_id),
                "type": "pb",
                "label": label,
                "image_url": f"{IMG_BASE}/npcdb/{pb.npc_id}.png",
                "ts": ts,
            })
    return out


@profiles_bp.get("/players/<int:player_id>")
async def player_profile(player_id: int):
    def _load():
        with db_session() as s:
            player = s.query(Player).filter(Player.player_id == player_id).first()
            if not player:
                return None
            partition = period_to_partition("all")

            group_id_rows = s.execute(
                text("SELECT group_id FROM user_group_association WHERE player_id = :pid"),
                {"pid": player_id},
            ).fetchall()
            groups = []
            for (gid,) in group_id_rows:
                if gid is None or gid <= 2:
                    continue
                g = s.query(Group).filter(Group.group_id == gid).first()
                if g:
                    groups.append({"id": gid, "name": g.group_name})

            recent = (
                s.query(NotifiedSubmission)
                .filter(or_(NotifiedSubmission.pb_id != None,  # noqa: E711
                            NotifiedSubmission.drop_id != None,
                            NotifiedSubmission.clog_id != None))
                .filter(NotifiedSubmission.player_id == player_id)
                .order_by(NotifiedSubmission.date_added.desc())
                .limit(10)
                .all()
            )
            submissions = _build_submissions(recent, s, partition)

            loot = player_month_total(player_id, partition)
            rank = player_global_rank(player_id, partition)

            payload = {
                "id": player_id,
                "name": player.player_name,
                "total_loot": money(loot),
                "groups": groups,
                "recent_submissions": submissions,
            }
            if rank is not None:
                payload["global_rank"] = rank
            return payload

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Player not found", f"No player with id {player_id}")
    return jsonify(payload)


@profiles_bp.get("/groups/<int:group_id>")
async def group_profile(group_id: int):
    def _load():
        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                return None
            partition = period_to_partition("all")

            player_ids = [
                pid for (pid,) in s.query(Player.player_id).join(Player.groups)
                .filter(Group.group_id == group_id).all()
            ]
            member_count = len(player_ids)
            monthly = player_list_loot_sum(player_ids, partition)

            # Global rank via the cached cross-group totals.
            rank = None
            try:
                totals = _compute_group_totals(partition)
                for idx, (gid, _n, _t, _m) in enumerate(totals, start=1):
                    if gid == group_id:
                        rank = idx
                        break
            except Exception:
                rank = None

            # Top player = highest-ranked member in the group sorted set.
            top_player = None
            conn = _rc()
            if conn is not None:
                try:
                    raw = conn.zrevrange(leaderboard_key(partition, group_id=group_id), 0, 0, withscores=True)
                    if raw:
                        pid = decode_member(raw[0][0])
                        if pid is not None:
                            tp = s.query(Player).filter(Player.player_id == pid).first()
                            if tp:
                                top_player = {
                                    "id": pid,
                                    "name": tp.player_name,
                                    "total_loot": money(int(float(raw[0][1]))),
                                }
                except Exception:
                    top_player = None

            recent = (
                s.query(NotifiedSubmission)
                .filter(or_(NotifiedSubmission.pb_id != None,  # noqa: E711
                            NotifiedSubmission.drop_id != None,
                            NotifiedSubmission.clog_id != None))
                .filter(NotifiedSubmission.group_id == group_id)
                .order_by(NotifiedSubmission.date_added.desc())
                .limit(10)
                .all()
            )
            submissions = _build_submissions(recent, s, partition)

            payload = {
                "id": group_id,
                "name": group.group_name,
                "member_count": member_count,
                "monthly_loot": money(monthly),
                "recent_submissions": submissions,
            }
            if group.description:
                payload["description"] = group.description
            if group.invite_url:
                payload["discord_url"] = group.invite_url
            if rank is not None:
                payload["global_rank"] = rank
            if top_player is not None:
                payload["top_player"] = top_player
            return payload

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Group not found", f"No group with id {group_id}")
    return jsonify(payload)


@profiles_bp.get("/groups/<int:group_id>/members")
async def group_members(group_id: int):
    page, limit = parse_page(request)

    def _load():
        with db_session() as s:
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                return None
            partition = period_to_partition("all")
            rows = (
                s.query(Player.player_id, Player.player_name)
                .join(Player.groups)
                .filter(Group.group_id == group_id)
                .all()
            )
            members = []
            for pid, name in rows:
                members.append({
                    "id": pid,
                    "name": name,
                    "total_loot": money(player_month_total(pid, partition)),
                    "hidden": False,
                })
            members.sort(key=lambda m: m["total_loot"]["value"], reverse=True)
            total = len(members)
            start = (page - 1) * limit
            window = members[start:start + limit]
            return {"members": window, "meta": {"page": page, "limit": limit, "total": total}}

    payload = await asyncio.to_thread(_load)
    if payload is None:
        return problem(404, "Group not found", f"No group with id {group_id}")
    return jsonify(payload)
