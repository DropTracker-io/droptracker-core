"""Group always-announce list — items and NPCs a clan always wants posted.

  GET    /api/v1/groups/{id}/notification-always-list          (session + group admin)
  POST   /api/v1/groups/{id}/notification-always-list          { entry_type, name, game_id? }
  DELETE /api/v1/groups/{id}/notification-always-list/{entryId}

The inverse of the notification blacklist (``group_blacklist.py``), sharing its
name normalization so the two lists can never disagree about what a name means.
An entry here widens exactly one gate: a **drop** of a listed item — or from a
listed NPC — is announced in the group's Discord even when it falls below the
group's ``minimum_value_to_notify``. Nothing else changes: screenshot
requirements still apply to the forced post, the blacklist wins outright when a
name is somehow on both lists, and the other announcement types (clogs, pets,
CAs, PBs) keep their own toggles.

This exists for the "notable" zero-value items the plugin already
force-screenshots (untradeable kits, dyes, pieces): the screenshot arrives and
uploads, but nothing ever told the clan about it. No ``region`` entries — a
drop's value gate is the only gate this list bypasses, and drops carry no
region.

Names are normalized into ``match_key`` on write with the *same* function the
drop pipeline matches with (``db.notification_always_list.entry_key``), so
"Twisted ancestral colour kit" typed any way is one entry that actually fires.
The picker feeds ``game_id`` for the icon and nothing else; a hand-typed name
the catalog has not seen yet is still a valid entry.
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify
from sqlalchemy.exc import IntegrityError

from db import AuditLog
from db.models import GroupNotificationAlwaysList
from db.notification_always_list import ENTRY_TYPES, entry_key
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    json_body,
    load_user,
    manageable_guild_ids,
)

group_always_list_bp = Blueprint("v1_group_always_list", __name__)

# Same ceiling as the blacklist: the list is read on the drop hot path, and a
# clan force-announcing hundreds of things has a minimum-value problem rather
# than an always-list problem.
MAX_ENTRIES_PER_GROUP = 250

# ``group_notification_always_list.entry_name`` is VARCHAR(125), matching
# ``items.item_name``. Longer input is a typo or an attack, not a real name.
MAX_NAME_LENGTH = 125


def _serialize(row: GroupNotificationAlwaysList) -> dict:
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
        s.query(GroupNotificationAlwaysList)
        .filter(GroupNotificationAlwaysList.group_id == group_id)
        .order_by(
            GroupNotificationAlwaysList.entry_type,
            GroupNotificationAlwaysList.entry_name,
        )
        .all()
    )
    return [_serialize(r) for r in rows]


def _payload(s, group_id: int) -> dict:
    """The whole list, exactly as the GET returns it — mutations echo it back
    so the editor replaces its state instead of re-fetching (the same contract
    the blacklist editor depends on; see group_blacklist._payload)."""
    return {"entries": _entries(s, group_id), "limit": MAX_ENTRIES_PER_GROUP}


@group_always_list_bp.get("/groups/<int:group_id>/notification-always-list")
async def list_always_list(group_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            return _payload(s, group_id)

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@group_always_list_bp.post("/groups/<int:group_id>/notification-always-list")
async def add_always_list_entry(group_id: int):
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

    # The pipeline matches on this; a name that normalizes to nothing
    # (punctuation only, or a placeholder like "Unknown") would match either
    # everything or nothing depending on the payload, so it is refused at the door.
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
                s.query(GroupNotificationAlwaysList)
                .filter(
                    GroupNotificationAlwaysList.group_id == group_id,
                    GroupNotificationAlwaysList.entry_type == entry_type,
                    GroupNotificationAlwaysList.match_key == match_key,
                )
                .first()
            )
            if existing is not None:
                # Idempotent: re-adding what is already listed is a no-op, not
                # a 409 the UI would have to explain.
                return {"entry": _serialize(existing), **_payload(s, group_id)}

            count = (
                s.query(GroupNotificationAlwaysList)
                .filter(GroupNotificationAlwaysList.group_id == group_id)
                .count()
            )
            if count >= MAX_ENTRIES_PER_GROUP:
                abort_problem(
                    409,
                    "List full",
                    f"A group may always-announce at most {MAX_ENTRIES_PER_GROUP} entries. "
                    "Remove one first, or lower the minimum notification value instead.",
                )

            row = GroupNotificationAlwaysList(
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
                    action="notification_always_list.add",
                    target=f"group_notification_always_list.{entry_type}",
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
                    s.query(GroupNotificationAlwaysList)
                    .filter(
                        GroupNotificationAlwaysList.group_id == group_id,
                        GroupNotificationAlwaysList.entry_type == entry_type,
                        GroupNotificationAlwaysList.match_key == match_key,
                    )
                    .first()
                )
                if row is None:
                    raise
            return {"entry": _serialize(row), **_payload(s, group_id)}

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@group_always_list_bp.delete(
    "/groups/<int:group_id>/notification-always-list/<int:entry_id>"
)
async def delete_always_list_entry(group_id: int, entry_id: int):
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            user = load_user(s, user_id)
            assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
            row = (
                s.query(GroupNotificationAlwaysList)
                .filter(
                    GroupNotificationAlwaysList.id == entry_id,
                    # Scoped to the group in the path: an admin of group A must
                    # not be able to delete group B's row by guessing its id.
                    GroupNotificationAlwaysList.group_id == group_id,
                )
                .first()
            )
            if row is None:
                abort_problem(404, "Entry not found", f"No always-announce entry {entry_id} for this group.")
            removed_type, removed_name = row.entry_type, row.entry_name
            s.delete(row)
            s.add(
                AuditLog(
                    actor_user_id=user_id,
                    group_id=group_id,
                    action="notification_always_list.remove",
                    target=f"group_notification_always_list.{removed_type}",
                    before=removed_name,
                    after=None,
                )
            )
            s.commit()
            return _payload(s, group_id)

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))
