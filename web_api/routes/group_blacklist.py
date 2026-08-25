"""Group notification blacklist — items/NPCs a clan never wants announced.

  GET    /api/v1/groups/{id}/notification-blacklist          (session + group admin)
  POST   /api/v1/groups/{id}/notification-blacklist          { entry_type, name, game_id? }
  DELETE /api/v1/groups/{id}/notification-blacklist/{entryId}

A blacklist entry withholds the **Discord notification** only. The drop, pet,
collection-log slot or personal best is still recorded, still scored, still on
the lootboard and the leaderboards — clans asked to stop seeing the 47th "Bones
from Barrows" in their feed channel, not to stop counting it. Both the enqueue
gate (``data/submissions/common.create_notification``) and the send-side guard
(``services/notification_service``) read these rows through the shared matcher
in ``db.notification_blacklist``.

Names are normalized into ``match_key`` on write with the *same* function the
pipeline matches with, so "Twisted Bow", "twisted bow" and "Twisted_bow" are
one entry rather than three near-misses that each fail to fire. NPC keys fold
raid mode-variants into their base raid, so blacklisting "Chambers of Xeric"
also silences Challenge Mode (but not the other way round).

The picker on the settings page feeds ``game_id`` from ``/events/meta/items``
and ``/events/meta/npcs``; it is used for the icon and nothing else, so a
hand-typed name for something the catalog has not seen yet is still a valid
entry.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify
from sqlalchemy.exc import IntegrityError

from db import AuditLog
from db.models import GroupNotificationBlacklist
from db.notification_blacklist import ENTRY_TYPES, entry_key
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

group_blacklist_bp = Blueprint("v1_group_blacklist", __name__)

# A generous ceiling, not a design constraint: the list is read on the
# notification hot path, and a clan with hundreds of muted items has a
# minimum-value problem rather than a blacklist problem.
MAX_ENTRIES_PER_GROUP = 250

# ``group_notification_blacklist.entry_name`` is VARCHAR(125), matching
# ``items.item_name``. Longer input is a typo or an attack, not a real name.
MAX_NAME_LENGTH = 125


def _serialize(row: GroupNotificationBlacklist) -> dict:
    return {
        "id": int(row.id),
        "entry_type": row.entry_type,
        "name": row.entry_name,
        "match_key": row.match_key,
        "game_id": int(row.game_id) if row.game_id is not None else None,
        "added_at": row.date_added.isoformat() if row.date_added else None,
    }


def _entries(s, group_id: int) -> list[dict]:
    rows = (
        s.query(GroupNotificationBlacklist)
        .filter(GroupNotificationBlacklist.group_id == group_id)
        .order_by(
            GroupNotificationBlacklist.entry_type,
            GroupNotificationBlacklist.entry_name,
        )
        .all()
    )
    return [_serialize(r) for r in rows]


@group_blacklist_bp.get("/groups/<int:group_id>/notification-blacklist")
async def list_blacklist(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            return {"entries": _entries(s, group_id), "limit": MAX_ENTRIES_PER_GROUP}

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@group_blacklist_bp.post("/groups/<int:group_id>/notification-blacklist")
async def add_blacklist_entry(group_id: int):
    user_id = current_user_id()
    body = await json_body()

    entry_type = str(body.get("entry_type") or "").strip().lower()
    if entry_type not in ENTRY_TYPES:
        abort_problem(
            422,
            "Invalid entry type",
            f"'entry_type' must be one of: {', '.join(ENTRY_TYPES)}.",
        )

    name = str(body.get("name") or "").strip()
    if not name:
        abort_problem(422, "Invalid name", "'name' is required.")
    if len(name) > MAX_NAME_LENGTH:
        abort_problem(
            422, "Name too long", f"Names are limited to {MAX_NAME_LENGTH} characters."
        )

    # The pipeline matches on this; a name that normalizes to nothing (punctuation
    # only, or a placeholder like "Unknown") would match either everything or
    # nothing depending on the payload, so it is refused at the door.
    match_key = entry_key(entry_type, name)
    if not match_key:
        abort_problem(
            422,
            "Unrecognized name",
            f"'{name}' cannot be matched against submissions. Pick an entry from "
            "the search results, or use the exact in-game name.",
        )

    raw_game_id = body.get("game_id")
    game_id = None
    if raw_game_id is not None:
        try:
            game_id = int(raw_game_id)
        except (TypeError, ValueError):
            abort_problem(422, "Invalid game id", "'game_id' must be an integer or null.")

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)

            existing = (
                s.query(GroupNotificationBlacklist)
                .filter(
                    GroupNotificationBlacklist.group_id == group_id,
                    GroupNotificationBlacklist.entry_type == entry_type,
                    GroupNotificationBlacklist.match_key == match_key,
                )
                .first()
            )
            if existing is not None:
                # Idempotent: re-adding what is already muted is a no-op, not a
                # 409 the UI would have to explain.
                return {"entry": _serialize(existing), "entries": _entries(s, group_id)}

            count = (
                s.query(GroupNotificationBlacklist)
                .filter(GroupNotificationBlacklist.group_id == group_id)
                .count()
            )
            if count >= MAX_ENTRIES_PER_GROUP:
                abort_problem(
                    409,
                    "Blacklist full",
                    f"A group may blacklist at most {MAX_ENTRIES_PER_GROUP} entries. "
                    "Remove one first, or raise the minimum notification value instead.",
                )

            row = GroupNotificationBlacklist(
                group_id=group_id,
                entry_type=entry_type,
                entry_name=name,
                match_key=match_key,
                game_id=game_id,
                added_by_user_id=user_id,
            )
            s.add(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="notification_blacklist.add",
                    target=f"group_notification_blacklist.{entry_type}",
                    before=None,
                    after=name,
                )
            )
            try:
                s.commit()
            except IntegrityError:
                # Lost a race with a concurrent add of the same entry; the
                # outcome the caller wanted is now true either way.
                s.rollback()
                row = (
                    s.query(GroupNotificationBlacklist)
                    .filter(
                        GroupNotificationBlacklist.group_id == group_id,
                        GroupNotificationBlacklist.entry_type == entry_type,
                        GroupNotificationBlacklist.match_key == match_key,
                    )
                    .first()
                )
                if row is None:
                    raise
            return {"entry": _serialize(row), "entries": _entries(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@group_blacklist_bp.delete("/groups/<int:group_id>/notification-blacklist/<int:entry_id>")
async def delete_blacklist_entry(group_id: int, entry_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            row = (
                s.query(GroupNotificationBlacklist)
                .filter(
                    GroupNotificationBlacklist.id == entry_id,
                    # Scoped to the group in the path: an admin of group A must
                    # not be able to delete group B's row by guessing its id.
                    GroupNotificationBlacklist.group_id == group_id,
                )
                .first()
            )
            if row is None:
                abort_problem(404, "Entry not found", f"No blacklist entry {entry_id} for this group.")
            removed_type, removed_name = row.entry_type, row.entry_name
            s.delete(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="notification_blacklist.remove",
                    target=f"group_notification_blacklist.{removed_type}",
                    before=removed_name,
                    after=None,
                )
            )
            s.commit()
            return {"entries": _entries(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))
