"""Turn bot-side admin entries into website admin grants (ticket 479).

A group's admins live in two stores: the Discord bot's ``authed_users`` config
(a JSON list of Discord IDs) and the website's ``group_admins`` rows, which are
keyed by ``user_id``. ``resolve_group_role`` reads only the second. When an
owner adds someone by Discord ID before that person has a ``users`` row, there
is no ``user_id`` to grant, so only the bot list is written, and the roster
shows them as "Bot commands only — website access activates when they sign
in here".

This module is what makes that sentence true: ``activate_pending_grants`` runs
at sign-in and gives the person a ``group_admins`` row for every group whose
``authed_users`` lists their Discord ID. ``scripts/backfill_pending_group_admins``
uses the same functions for people who were stranded before this existed.

Promotion only ever adds ``admin`` rows. It never touches an existing grant
(an owner stays owner), and removing someone through the roster deletes both
stores, so a removed admin is not re-promoted at their next sign-in.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

AUTHED_USERS_KEY = "authed_users"
AUDIT_ACTION = "group.admin.activate"


def parse_authed_ids(raw) -> list[str]:
    """Discord IDs from an ``authed_users`` value, as strings.

    Tolerates every shape the column has held: a JSON list of strings (web), a
    JSON list of ints (bot era), a bare int, and junk, which yields ``[]``.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, int) and not isinstance(raw, bool):
        return [str(raw)]
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, int) and not isinstance(data, bool):
        return [str(data)]
    if not isinstance(data, list):
        return []
    return [str(v).strip() for v in data if v not in (None, "", False)]


def row_authed_ids(row) -> list[str]:
    """``authed_users`` IDs from a config row (short lists in config_value,
    long ones spilled to long_value)."""
    return parse_authed_ids(
        getattr(row, "config_value", None) or getattr(row, "long_value", None)
    )


def pending_group_ids(
    discord_id: str,
    authed_rows: Iterable,
    granted_group_ids: Iterable[int],
    live_group_ids: Optional[Iterable[int]] = None,
) -> list[int]:
    """Groups whose ``authed_users`` lists ``discord_id`` but which have no
    ``group_admins`` row for this user yet. Pure: callers supply the rows.

    ``live_group_ids``, when given, drops config rows left behind by deleted
    groups.
    """
    did = str(discord_id).strip()
    granted = set(granted_group_ids)
    live = set(live_group_ids) if live_group_ids is not None else None
    out: list[int] = []
    for row in authed_rows:
        gid = row.group_id
        if gid in granted or gid in out:
            continue
        if live is not None and gid not in live:
            continue
        if did in row_authed_ids(row):
            out.append(gid)
    return sorted(out)


def find_pending_grants(s, user) -> list[int]:
    """Group ids ``user`` should be an admin of but has no web grant for."""
    from db import Group, GroupAdmin, GroupConfiguration

    discord_id = str(getattr(user, "discord_id", "") or "").strip()
    if not discord_id.isdigit():
        return []

    # The LIKE narrows a few hundred config rows to the handful mentioning this
    # snowflake; parse_authed_ids then does the exact match, so a substring hit
    # inside a longer ID never counts.
    pattern = f"%{discord_id}%"
    rows = (
        s.query(GroupConfiguration)
        .filter(
            GroupConfiguration.config_key == AUTHED_USERS_KEY,
            (GroupConfiguration.config_value.like(pattern))
            | (GroupConfiguration.long_value.like(pattern)),
        )
        .all()
    )
    if not rows:
        return []

    gids = {r.group_id for r in rows}
    granted = [
        gid for (gid,) in s.query(GroupAdmin.group_id)
        .filter(GroupAdmin.user_id == user.user_id, GroupAdmin.group_id.in_(gids))
        .all()
    ]
    live = [
        gid for (gid,) in s.query(Group.group_id).filter(Group.group_id.in_(gids)).all()
    ]
    return pending_group_ids(discord_id, rows, granted, live)


def grant_pending(s, user, group_ids: Iterable[int]) -> list[int]:
    """Add ``admin`` grants + audit rows for ``group_ids``. Caller commits."""
    from db import GroupAdmin
    from db.models import AuditLog

    added: list[int] = []
    for gid in group_ids:
        s.add(GroupAdmin(group_id=gid, user_id=user.user_id, role="admin", granted_by=None))
        s.add(AuditLog(
            actor_user_id=user.user_id,
            group_id=gid,
            action=AUDIT_ACTION,
            target=str(user.user_id),
            after=f"user={user.user_id} discord={user.discord_id} role=admin "
                  "source=authed_users",
        ))
        added.append(gid)
    return added


def activate_pending_grants(user_id: int) -> list[int]:
    """Sign-in hook: promote this user's pending Discord-ID admin entries.

    Best-effort by contract. It must never fail a login, and a lost race on
    ``uix_group_admin`` (two tabs signing in at once) just means the other
    request already did the work.
    """
    from sqlalchemy.exc import IntegrityError

    from db import User
    from web_api.common import db_session

    with db_session() as s:
        user = s.query(User).filter(User.user_id == user_id).first()
        if user is None:
            return []
        pending = find_pending_grants(s, user)
        if not pending:
            return []
        added = grant_pending(s, user, pending)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return []
        return added
