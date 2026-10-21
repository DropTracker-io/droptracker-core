"""Self-serve data removal: request, follow and cancel (web137a).

The rules live in ``services/data_removal.py``; these routes only resolve who
is asking and translate ``RemovalError`` into problem responses.

* ``GET  /me/data-removal``                   the caller's requests
* ``POST /me/players/<id>/data-removal``      ask to erase one claimed account
* ``POST /me/data-removal/<id>/cancel``       withdraw before the purge
* ``GET  /groups/<id>/data-removal``          the group's open request (admins)
* ``POST /groups/<id>/data-removal``          owner asks to erase the group
"""
from __future__ import annotations

import asyncio

from quart import Blueprint, jsonify

from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    is_group_owner,
    json_body,
    load_user,
    manageable_guild_ids,
)

data_removal_bp = Blueprint("data_removal", __name__)


def _run(fn):
    """Run ``fn(s)`` in a session, committing on success; RemovalError → problem."""
    from services.data_removal import RemovalError

    def _inner():
        with db_session() as s:
            try:
                out = fn(s)
                s.commit()
                return out
            except RemovalError as e:
                s.rollback()
                abort_problem(e.status, e.title, e.detail)

    return asyncio.to_thread(_inner)


@data_removal_bp.get("/me/data-removal")
async def my_requests():
    user_id = current_user_id()

    def _load(s):
        from db.models.data_removal import DataRemovalRequest
        from services.data_removal import status_payload

        rows = (s.query(DataRemovalRequest)
                .filter(DataRemovalRequest.requested_by_user_id == user_id)
                .order_by(DataRemovalRequest.id.desc()).limit(50).all())
        return {"requests": [status_payload(r) for r in rows]}

    return private_no_store(jsonify(await _run(_load)))


@data_removal_bp.post("/me/players/<int:player_id>/data-removal")
async def request_player(player_id: int):
    user_id = current_user_id()
    body = await json_body()
    typed = str(body.get("confirm_name") or "")

    def _apply(s):
        from services.data_removal import request_player_removal, status_payload

        user = load_user(s, user_id)
        req = request_player_removal(s, user, player_id, typed)
        s.flush()
        return status_payload(req)

    return private_no_store(jsonify(await _run(_apply))), 201


@data_removal_bp.post("/me/data-removal/<int:request_id>/cancel")
async def cancel_request(request_id: int):
    user_id = current_user_id()

    def _apply(s):
        from services.data_removal import cancel, status_payload
        from services.ticket_transcripts import add_system_message

        req = cancel(s, request_id, user_id)
        if req.ticket_id:
            # add_system_message has its own session; record after commit.
            s.commit()
            add_system_message(req.ticket_id,
                               f"The requester cancelled the removal of **{req.subject_name}**. "
                               "Nothing will be erased.")
        return status_payload(req)

    return private_no_store(jsonify(await _run(_apply)))


@data_removal_bp.get("/groups/<int:group_id>/data-removal")
async def group_request(group_id: int):
    user_id = current_user_id()

    def _load(s):
        from services.data_removal import KIND_GROUP, open_request, status_payload

        user = load_user(s, user_id)
        assert_group_admin(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
        req = open_request(s, KIND_GROUP, group_id)
        return {"request": status_payload(req) if req else None,
                "can_request": is_group_owner(s, user_id, group_id, user=user)}

    return private_no_store(jsonify(await _run(_load)))


@data_removal_bp.post("/groups/<int:group_id>/data-removal")
async def request_group(group_id: int):
    user_id = current_user_id()
    body = await json_body()
    typed = str(body.get("confirm_name") or "")

    def _apply(s):
        from services.data_removal import request_group_removal, status_payload

        user = load_user(s, user_id)
        req = request_group_removal(s, user, group_id, typed,
                                    is_owner=is_group_owner(s, user_id, group_id, user=user))
        s.flush()
        return status_payload(req)

    return private_no_store(jsonify(await _run(_apply))), 201
