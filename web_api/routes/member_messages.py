"""Members' own death messages — the member's editor and the leader's review.

  GET    /api/v1/me/death-messages                                      (session)
  PUT    /api/v1/me/players/{playerId}/death-messages                   { messages: [...] }
  GET    /api/v1/groups/{id}/member-death-messages                      (session + group admin)
  PUT    /api/v1/groups/{id}/member-death-messages/{playerId}/block
  DELETE /api/v1/groups/{id}/member-death-messages/{playerId}/block

A member writes up to five lines for each of their accounts; one is picked per
death in every group that opted in with ``allow_member_death_messages``. The
member's payload lists each account's groups with whether its message would be
posted there, so the editor can say "posting in Clan A, not in Clan B" instead
of leaving them to wonder why nothing changed.

The group side is moderation, not editing: a leader sees what each member wrote
and can block a member, which keeps that member's own lines out of the group's
channels and changes nothing anywhere else. Blocks work whether or not the
setting is on, so a leader can deal with someone before opening it up.

Every rule — what may be saved, the limits, the placeholders — comes from
``db.member_messages``, shared with the plugin endpoint, the Discord panel and
the sender. Mutations echo the whole payload back so an editor replaces its
state instead of re-fetching.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db import AuditLog, Player
from db.member_messages import (
    MESSAGE_TYPE_DEATH,
    VIA_WEB,
    MemberMessageError,
    death_message_group_status,
    group_allows_member_death_messages,
    limits_payload,
    load_member_message_row,
    parse_stored_messages,
    store_member_messages,
)
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

member_messages_bp = Blueprint("v1_member_messages", __name__)


def _iso(value):
    return value.isoformat() if value is not None else None


# --------------------------------------------------------------------------- #
# The member's own editor
# --------------------------------------------------------------------------- #
def _my_payload(s, user) -> dict:
    players = (
        s.query(Player)
        .filter(Player.user_id == user.user_id)
        .order_by(Player.player_name)
        .all()
    )
    accounts = []
    for player in players:
        row = load_member_message_row(s, player.player_id, MESSAGE_TYPE_DEATH)
        accounts.append({
            "id": int(player.player_id),
            "name": player.player_name,
            "messages": parse_stored_messages(row.messages) if row is not None else [],
            "updated_at": _iso(row.updated_at) if row is not None else None,
            "groups": death_message_group_status(s, player.player_id),
        })
    return {**limits_payload(), "players": accounts}


@member_messages_bp.get("/me/death-messages")
async def get_my_death_messages():
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            return _my_payload(s, user) if user else None

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(payload))


@member_messages_bp.put("/me/players/<int:player_id>/death-messages")
async def put_my_death_messages(player_id: int):
    """Replace one linked account's death messages. An empty list clears them."""
    user_id = current_user_id()
    body = await json_body()
    messages = body.get("messages")
    if not isinstance(messages, list):
        abort_problem(422, "Invalid value", "'messages' must be a list of text lines.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return None
            player = (
                s.query(Player)
                .filter(Player.player_id == player_id, Player.user_id == user.user_id)
                .first()
            )
            if not player:
                abort_problem(404, "Player not found", "That account is not linked to you.")
            try:
                store_member_messages(
                    s, player_id, messages, via=VIA_WEB, user_id=user.user_id
                )
            except MemberMessageError as exc:
                s.rollback()
                abort_problem(422, "Invalid death message", str(exc))
            s.commit()
            return _my_payload(s, user)

    payload = await asyncio.to_thread(_apply)
    if payload is None:
        abort_problem(401, "Not authenticated", "User not found for this session.")
    return private_no_store(jsonify(payload))


# --------------------------------------------------------------------------- #
# The group leader's review
# --------------------------------------------------------------------------- #
_MEMBER_MESSAGES_SQL = text(
    """
    SELECT p.player_id, p.player_name, m.messages, m.updated_at
    FROM player_custom_messages m
    JOIN players p ON p.player_id = m.player_id
    JOIN user_group_association uga
      ON uga.player_id = m.player_id AND uga.group_id = :group_id
    WHERE m.message_type = :message_type
    """
)

_BLOCKED_SQL = text(
    """
    SELECT b.player_id, p.player_name, b.created_at
    FROM group_member_message_blocks b
    JOIN players p ON p.player_id = b.player_id
    WHERE b.group_id = :group_id
    """
)


def _group_payload(s, group_id: int) -> dict:
    members: dict[int, dict] = {}
    rows = s.execute(
        _MEMBER_MESSAGES_SQL, {"group_id": group_id, "message_type": MESSAGE_TYPE_DEATH}
    ).all()
    for player_id, name, raw, updated_at in rows:
        messages = parse_stored_messages(raw)
        if not messages:
            continue
        members[int(player_id)] = {
            "id": int(player_id),
            "name": name,
            "messages": messages,
            "updated_at": _iso(updated_at),
            "blocked": False,
            "blocked_at": None,
        }
    for player_id, name, created_at in s.execute(_BLOCKED_SQL, {"group_id": group_id}).all():
        entry = members.setdefault(int(player_id), {
            "id": int(player_id),
            "name": name,
            "messages": [],
            "updated_at": None,
        })
        entry["blocked"] = True
        entry["blocked_at"] = _iso(created_at)
    return {
        "enabled": group_allows_member_death_messages(s, group_id),
        "members": sorted(members.values(), key=lambda m: (m["name"] or "").lower()),
    }


def _assert_admin(s, user_id: int, group_id: int):
    user = load_user(s, user_id)
    assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)


@member_messages_bp.get("/groups/<int:group_id>/member-death-messages")
async def get_group_member_death_messages(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            _assert_admin(s, user_id, group_id)
            return _group_payload(s, group_id)

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


def _member_name(s, group_id: int, player_id: int):
    """The player's name if they belong to this group, else ``None``."""
    row = s.execute(
        text(
            "SELECT p.player_name FROM user_group_association uga "
            "JOIN players p ON p.player_id = uga.player_id "
            "WHERE uga.group_id = :group_id AND uga.player_id = :player_id LIMIT 1"
        ),
        {"group_id": group_id, "player_id": player_id},
    ).first()
    return row[0] if row is not None else None


@member_messages_bp.put("/groups/<int:group_id>/member-death-messages/<int:player_id>/block")
async def block_member_death_messages(group_id: int, player_id: int):
    user_id = current_user_id()

    def _apply():
        from db.models import GroupMemberMessageBlock

        with db_session() as s:
            _assert_admin(s, user_id, group_id)
            name = _member_name(s, group_id, player_id)
            if name is None:
                abort_problem(404, "Member not found", "That player is not a member of this group.")
            exists = (
                s.query(GroupMemberMessageBlock.id)
                .filter(
                    GroupMemberMessageBlock.group_id == group_id,
                    GroupMemberMessageBlock.player_id == player_id,
                )
                .first()
            )
            if exists is None:
                s.add(GroupMemberMessageBlock(
                    group_id=group_id, player_id=player_id, created_by_user_id=user_id,
                ))
                s.add(AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="member_messages.block",
                    target=f"group_member_message_blocks.{player_id}",
                    before=None,
                    after=name,
                ))
                try:
                    s.commit()
                except IntegrityError:
                    # A concurrent block of the same member won; the member is
                    # blocked either way, which is what the caller asked for.
                    s.rollback()
            return _group_payload(s, group_id)

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@member_messages_bp.delete("/groups/<int:group_id>/member-death-messages/<int:player_id>/block")
async def unblock_member_death_messages(group_id: int, player_id: int):
    user_id = current_user_id()

    def _apply():
        from db.models import GroupMemberMessageBlock

        with db_session() as s:
            _assert_admin(s, user_id, group_id)
            row = (
                s.query(GroupMemberMessageBlock)
                .filter(
                    GroupMemberMessageBlock.group_id == group_id,
                    GroupMemberMessageBlock.player_id == player_id,
                )
                .first()
            )
            if row is not None:
                # Unblocking works even for someone who has since left the
                # group, so a stale block can always be cleared.
                name = s.query(Player.player_name).filter(Player.player_id == player_id).scalar()
                s.delete(row)
                s.add(AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="member_messages.unblock",
                    target=f"group_member_message_blocks.{player_id}",
                    before=name,
                    after=None,
                ))
                s.commit()
            return _group_payload(s, group_id)

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))
