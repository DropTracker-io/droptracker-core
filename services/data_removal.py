"""Self-serve data removal for players and groups (web137a).

The flow, the same for both kinds:

1. **Request** (site): the requester types the name to confirm. This writes a
   ``data_removal_requests`` row and opens a support ticket (status
   ``pending``; the webhook bot gives it a Discord channel). Players may only
   ask for accounts they have claimed; groups only by their owner.
2. **Verify** (ticket): claims are first-come and unverified, so staff check
   ownership before anything happens. A player types the request's phrase in
   the game chatbox and posts a screenshot of their whole client; a group's
   admins are added to the ticket and pinged to confirm. Staff then press
   Approve or Reject (``services/data_removal_discord.py``).
3. **Grace** (``GRACE``): an approved request is ``scheduled``. A player is
   hidden from every board at once; the request can still be cancelled (by
   the requester on the site, or by staff), which puts everything back.
4. **Purge** (``scripts/process_data_removals.py``, hourly timer): the data is
   written to a private JSON export, the bot's Discord posts about a player
   are anonymized (not deleted, so clan channel history stays readable), and
   the rows are erased with ``db/player_erasure.py`` / ``scripts/purge_group``.
5. **Export expiry** (``EXPORT_RETENTION``): the export exists only so a
   mistake can be undone by hand; the same run deletes it after a week.

Nothing blocks the data from coming back: a still-running plugin or a
tracked clan's WOM roster recreates the player (owner decision, 2026-10-20).
The docs page says so.
"""
from __future__ import annotations

import gzip
import json
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import text

from db.models.data_removal import (
    AWAITING,
    CANCELLED,
    COMPLETED,
    FAILED,
    KIND_GROUP,
    KIND_PLAYER,
    OPEN_STATUSES,
    REJECTED,
    SCHEDULED,
    DataRemovalRequest,
)

GRACE = timedelta(days=7)
EXPORT_RETENTION = timedelta(days=7)
EXPORT_DIR = os.getenv(
    "DATA_REMOVAL_EXPORT_DIR", "/store/droptracker/disc/private/data_removal_exports"
)


class RemovalError(Exception):
    """A request the caller may not make; ``status`` is the HTTP status."""

    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status, self.title, self.detail = status, title, detail


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def norm_name(name: str | None) -> str:
    """Case-folded, with OSRS's interchangeable space/'_'/'-' collapsed."""
    import re

    return re.sub(r"[ _\-]+", " ", (name or "").strip()).casefold()


def names_match(typed: str | None, actual: str | None) -> bool:
    return bool(actual) and norm_name(typed) == norm_name(actual)


def new_phrase() -> str:
    """What a player types in the chatbox to prove they control the account."""
    return f"DropTracker removal {secrets.randbelow(900000) + 100000}"


def ticket_opening_text(kind: str, subject_name: str, phrase: str | None) -> str:
    if kind == KIND_PLAYER:
        return (
            f"I'd like all of my DropTracker data for **{subject_name}** removed.\n\n"
            "To verify this is my account, I will log in to "
            f"{subject_name}, type `{phrase}` in the chatbox, and post a screenshot "
            "of my whole RuneLite client here."
        )
    return (
        f"I'd like the group **{subject_name}** and all of its data removed from "
        "DropTracker. The group's admins have been added to this ticket to confirm."
    )


def status_payload(req: DataRemovalRequest) -> dict:
    def ts(d):
        return int(d.timestamp()) if d else None

    return {
        "id": req.id,
        "kind": req.kind,
        "subject_id": req.subject_id,
        "subject_name": req.subject_name,
        "status": req.status,
        "ticket_id": req.ticket_id,
        "verification_phrase": req.verification_phrase if req.kind == KIND_PLAYER else None,
        "purge_after": ts(req.purge_after),
        "created_at": ts(req.created_at),
        "decided_at": ts(req.decided_at),
        "completed_at": ts(req.completed_at),
        "can_cancel": req.status in OPEN_STATUSES,
    }


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
def open_request(s, kind: str, subject_id: int) -> DataRemovalRequest | None:
    return (
        s.query(DataRemovalRequest)
        .filter(DataRemovalRequest.kind == kind,
                DataRemovalRequest.subject_id == subject_id,
                DataRemovalRequest.status.in_(OPEN_STATUSES))
        .first()
    )


def _open_ticket(s, user, kind: str, subject_name: str, body: str) -> int:
    from db.models import Ticket, TicketMessage

    ticket = Ticket(
        channel_id=None,
        type="players" if kind == KIND_PLAYER else "clans",
        created_by=user.user_id,
        status="pending",
        subject=f"Data removal: {subject_name}"[:255],
        last_reply_uid=str(getattr(user, "discord_id", "") or ""),
        # Verification can take a while; the inactivity reaper must not close
        # the ticket under a request that is waiting on it.
        autoclose_exempt=True,
    )
    s.add(ticket)
    s.flush()
    s.add(TicketMessage(
        ticket_id=ticket.ticket_id,
        discord_message_id=None,
        author_user_id=user.user_id,
        author_discord_id=str(getattr(user, "discord_id", "") or "0"),
        author_name=(getattr(user, "username", None) or f"user {user.user_id}")[:100],
        is_staff=False,
        is_bot=False,
        kind="message",
        origin="web",
        content=body,
        date_sent=datetime.now(),
    ))
    return ticket.ticket_id


def request_player_removal(s, user, player_id: int, typed_name: str) -> DataRemovalRequest:
    from db.models import Player

    player = s.query(Player).filter(Player.player_id == player_id).first()
    if player is None or player.user_id != user.user_id:
        raise RemovalError(404, "Not your account",
                           "You can only request removal of an account you have claimed.")
    if not names_match(typed_name, player.player_name):
        raise RemovalError(422, "Name doesn't match",
                           f"Type the account name exactly as shown: {player.player_name}")
    if open_request(s, KIND_PLAYER, player_id):
        raise RemovalError(409, "Already requested",
                           "A removal request for this account is already open.")
    phrase = new_phrase()
    req = DataRemovalRequest(
        kind=KIND_PLAYER, subject_id=player_id, subject_name=player.player_name,
        status=AWAITING, requested_by_user_id=user.user_id, verification_phrase=phrase,
    )
    req.ticket_id = _open_ticket(s, user, KIND_PLAYER, player.player_name,
                                 ticket_opening_text(KIND_PLAYER, player.player_name, phrase))
    s.add(req)
    _audit(s, user.user_id, None, "data_removal.request", req, f"player {player_id}")
    return req


def request_group_removal(s, user, group_id: int, typed_name: str,
                          *, is_owner: bool) -> DataRemovalRequest:
    from db.models import Group

    group = s.query(Group).filter(Group.group_id == group_id).first()
    if group is None:
        raise RemovalError(404, "Group not found", "No such group.")
    if group_id == 2:
        raise RemovalError(403, "Not allowed", "The global group cannot be removed.")
    if not is_owner:
        raise RemovalError(403, "Owner only", "Only the group's owner can ask for its removal.")
    if not names_match(typed_name, group.group_name):
        raise RemovalError(422, "Name doesn't match",
                           f"Type the group name exactly as shown: {group.group_name}")
    if open_request(s, KIND_GROUP, group_id):
        raise RemovalError(409, "Already requested",
                           "A removal request for this group is already open.")
    req = DataRemovalRequest(
        kind=KIND_GROUP, subject_id=group_id, subject_name=group.group_name,
        status=AWAITING, requested_by_user_id=user.user_id,
    )
    req.ticket_id = _open_ticket(s, user, KIND_GROUP, group.group_name,
                                 ticket_opening_text(KIND_GROUP, group.group_name, None))
    s.add(req)
    _audit(s, user.user_id, group_id, "data_removal.request", req, f"group {group_id}")
    return req


def group_admin_discord_ids(s, group_id: int) -> list[str]:
    """Everyone with admin rights on the group, as Discord ids: website
    grants plus the bot's ``authed_users`` list."""
    from db.models import GroupAdmin, GroupConfiguration, User

    ids = {
        str(d) for (d,) in s.query(User.discord_id)
        .join(GroupAdmin, GroupAdmin.user_id == User.user_id)
        .filter(GroupAdmin.group_id == group_id, User.discord_id.isnot(None)).all()
        if d
    }
    row = (s.query(GroupConfiguration)
           .filter(GroupConfiguration.group_id == group_id,
                   GroupConfiguration.config_key == "authed_users").first())
    raw = (row.config_value or row.long_value) if row is not None else None
    try:
        data = json.loads(raw) if raw else []
        if isinstance(data, (int, str)):
            data = [data]
        ids |= {str(v).strip() for v in data if str(v).strip().isdigit()}
    except Exception:
        pass
    return sorted(ids)


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #
def _get(s, request_id: int) -> DataRemovalRequest:
    req = s.query(DataRemovalRequest).filter(DataRemovalRequest.id == request_id).first()
    if req is None:
        raise RemovalError(404, "Not found", "No such removal request.")
    return req


def approve(s, request_id: int, staff_user_id: int | None, now: datetime | None = None):
    now = now or datetime.now()
    req = _get(s, request_id)
    if req.status != AWAITING:
        raise RemovalError(409, "Already decided", f"This request is {req.status}.")
    req.status = SCHEDULED
    req.decided_by_user_id = staff_user_id
    req.decided_at = now
    req.purge_after = now + GRACE
    if req.kind == KIND_PLAYER:
        prior = s.execute(text("SELECT hidden FROM players WHERE player_id = :p"),
                          {"p": req.subject_id}).scalar()
        req.prior_hidden = int(bool(prior))
        s.execute(text("UPDATE players SET hidden = 1 WHERE player_id = :p"),
                  {"p": req.subject_id})
    _audit(s, staff_user_id, req.subject_id if req.kind == KIND_GROUP else None,
           "data_removal.approve", req)
    return req


def reject(s, request_id: int, staff_user_id: int | None, now: datetime | None = None):
    req = _get(s, request_id)
    if req.status != AWAITING:
        raise RemovalError(409, "Already decided", f"This request is {req.status}.")
    req.status = REJECTED
    req.decided_by_user_id = staff_user_id
    req.decided_at = now or datetime.now()
    _audit(s, staff_user_id, None, "data_removal.reject", req)
    return req


def cancel(s, request_id: int, actor_user_id: int | None, *, staff: bool = False):
    """The requester (or staff) withdraws before the purge. A scheduled
    player's visibility goes back to what it was before approval."""
    req = _get(s, request_id)
    if not staff and req.requested_by_user_id != actor_user_id:
        raise RemovalError(403, "Not your request", "Only the person who asked can cancel it.")
    if req.status not in OPEN_STATUSES:
        raise RemovalError(409, "Too late", f"This request is already {req.status}.")
    if req.status == SCHEDULED and req.kind == KIND_PLAYER and req.prior_hidden is not None:
        s.execute(text("UPDATE players SET hidden = :h WHERE player_id = :p"),
                  {"h": req.prior_hidden, "p": req.subject_id})
    req.status = CANCELLED
    req.decided_at = datetime.now()
    _audit(s, actor_user_id, None, "data_removal.cancel", req)
    return req


def _audit(s, actor, group_id, action, req, after=None):
    try:
        from db.models import AuditLog

        s.flush()
        s.add(AuditLog(actor_user_id=actor, group_id=group_id, action=action,
                       target=f"data_removal:{req.id}", after=after or req.status))
    except Exception:
        pass  # the request row itself is the record; audit is a convenience


# --------------------------------------------------------------------------- #
# Purge (run by scripts/process_data_removals.py)
# --------------------------------------------------------------------------- #
def due(s, now: datetime | None = None) -> list[DataRemovalRequest]:
    return (
        s.query(DataRemovalRequest)
        .filter(DataRemovalRequest.status == SCHEDULED,
                DataRemovalRequest.purge_after <= (now or datetime.now()))
        .order_by(DataRemovalRequest.id)
        .all()
    )


def _export_path(req: DataRemovalRequest) -> str:
    os.makedirs(EXPORT_DIR, mode=0o700, exist_ok=True)
    return os.path.join(EXPORT_DIR, f"{req.id}-{req.kind}-{req.subject_id}.jsonl.gz")


def _write_export(path: str, fill) -> tuple[int, int]:
    """Write a gzip JSON-lines export only the service accounts can read."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
        rows = fill(lambda line: gz.write(line.encode()))
    return rows, os.path.getsize(path)


def export_group_rows(s, group_id: int, write) -> int:
    """Every group-scoped row purge_group deletes, plus the rows of the
    group's own events (by event_id)."""
    from scripts.purge_group import UNLINKED, _fk_tables

    n = 0

    def emit(table, rows):
        nonlocal n
        from db.player_erasure import _jsonable

        for r in rows:
            write(json.dumps({"table": table, "action": "delete",
                              "row": {k: _jsonable(v) for k, v in r._mapping.items()}},
                             separators=(",", ":")) + "\n")
            n += 1

    emit("groups", s.execute(text("SELECT * FROM `groups` WHERE group_id = :g"),
                             {"g": group_id}).all())
    for table, col in _fk_tables(s):
        emit(table, s.execute(text(f"SELECT * FROM `{table}` WHERE `{col}` = :g"),
                              {"g": group_id}).all())
    for table, col, extra in UNLINKED:
        emit(table, s.execute(text(f"SELECT * FROM `{table}` WHERE `{col}` = :g {extra}"),
                              {"g": group_id}).all())
    event_ids = s.execute(text("SELECT id FROM web_events WHERE group_id = :g"),
                          {"g": group_id}).scalars().all()
    if event_ids:
        tables = s.execute(text(
            "SELECT DISTINCT c.table_name FROM information_schema.columns c "
            "JOIN information_schema.tables t ON t.table_schema = c.table_schema "
            " AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE' "
            "WHERE c.table_schema = DATABASE() AND c.column_name = 'event_id'"
        )).scalars().all()
        from sqlalchemy import bindparam

        for table in sorted(tables):
            emit(table, s.execute(text(f"SELECT * FROM `{table}` WHERE event_id IN :ids")
                                  .bindparams(bindparam("ids", expanding=True)),
                                  {"ids": list(event_ids)}).all())
    return n


def run_player(s, req: DataRemovalRequest, *, apply: bool, bot_token: str | None) -> dict:
    from db import player_erasure as pe
    from services.discord_anonymize import anonymize_messages
    from utils.redis import redis_client

    plan = pe.plan_erasure(s, req.subject_id, None, keep_owner=True)
    if plan.player_name is None:
        return {"note": "player already gone"}
    if plan.refusals:
        raise RuntimeError("; ".join(plan.refusals))
    if not apply:
        return {"rows_to_delete": sum(plan.delete.values()),
                "event_rows_to_move": sum(plan.reassign.values()),
                "discord_posts": len(plan.discord_messages)}

    path = _export_path(req)
    rows, size = _write_export(path, lambda w: pe.export_rows(s, plan, w))
    req.export_path, req.export_bytes = path, size
    req.export_expires_at = datetime.now() + EXPORT_RETENTION

    discord = {}
    if bot_token and plan.discord_messages:
        discord = anonymize_messages(plan.discord_messages, [plan.player_name],
                                     bot_token, apply=True)
    done = pe.apply_erasure(s, plan)
    s.commit()
    redis = pe.purge_redis(redis_client.client, plan.player_id, None, apply=True)
    files = pe.purge_files(plan, apply=True)
    return {"exported_rows": rows, "discord": discord,
            "db": {k: v for k, v in done.items() if v}, "redis": redis, "files": files}


def run_group(s, req: DataRemovalRequest, *, apply: bool) -> dict:
    from scripts import purge_group as pg

    name = s.execute(text("SELECT group_name FROM `groups` WHERE group_id = :g"),
                     {"g": req.subject_id}).scalar()
    if name is None:
        return {"note": "group already gone"}
    if not apply:
        return {"group": name}
    path = _export_path(req)
    rows, size = _write_export(path, lambda w: export_group_rows(s, req.subject_id, w))
    req.export_path, req.export_bytes = path, size
    req.export_expires_at = datetime.now() + EXPORT_RETENTION
    s.commit()
    if not pg.purge_db(req.subject_id, name, apply=True):
        raise RuntimeError("purge_group refused (see its output)")
    pg.purge_redis(req.subject_id, apply=True)
    pg.purge_files(req.subject_id, apply=True)
    return {"exported_rows": rows, "group": name}


def run_due(s, *, apply: bool, bot_token: str | None = None,
            now: datetime | None = None) -> list[dict]:
    from services.ticket_transcripts import add_system_message

    out = []
    for req in due(s, now):
        try:
            if req.kind == KIND_PLAYER:
                result = run_player(s, req, apply=apply, bot_token=bot_token)
            else:
                result = run_group(s, req, apply=apply)
            if apply:
                req.status = COMPLETED
                req.completed_at = datetime.now()
                req.result = json.dumps(result, default=str)[:60000]
                s.commit()
                if req.ticket_id:
                    add_system_message(req.ticket_id, (
                        f"The data for **{req.subject_name}** has been removed. "
                        "A private backup is kept for 7 days in case of a mistake, "
                        "then deleted for good."))
        except Exception as e:
            s.rollback()
            result = {"error": f"{type(e).__name__}: {e}"}
            if apply:
                req.status = FAILED
                req.result = json.dumps(result)
                s.commit()
        out.append({"id": req.id, "kind": req.kind, "subject": req.subject_name, **result})
    return out


def expire_exports(s, *, apply: bool, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    rows = (s.query(DataRemovalRequest)
            .filter(DataRemovalRequest.export_path.isnot(None),
                    DataRemovalRequest.export_expires_at <= now).all())
    gone = []
    for req in rows:
        gone.append(req.export_path)
        if apply:
            try:
                os.unlink(req.export_path)
            except FileNotFoundError:
                pass
            req.export_path = None
    if apply:
        s.commit()
    return gone
