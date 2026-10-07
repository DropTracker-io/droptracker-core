"""Clan bank (web131a): a group-level GP ledger.

  GET    /api/v1/groups/{id}/bank                      -> headline + donors (public)
  GET    /api/v1/groups/{id}/bank/entries              -> full ledger page (admin)
         ?status=pending|confirmed|rejected|void|all&before_id=&limit=
  POST   /api/v1/groups/{id}/bank/entries  { kind, amount, player_id?|rsn?,
                                             note?, proof_key?, event_id?,
                                             status? }          -> { id }
  PATCH  /api/v1/groups/{id}/bank/entries/{eid} { amount?, status?, note?,
                                                  review_note?, proof_key?,
                                                  player_id?, rsn? } -> { ok }
  DELETE /api/v1/groups/{id}/bank/entries/{eid}       -> { ok, voided }
  POST   /api/v1/groups/{id}/bank/transfer { event_id, amount, note? } -> { id }

Amounts are entered as positive GP; the kind decides the sign stored (see
``services/group_bank.signed_amount``). Adjustments carry their own sign.
The balance is the sum of confirmed rows.

A **transfer** moves GP from the bank into one of the group's own event prize
pots: it writes a confirmed ``event_transfer`` row here and a paid donation in
``web_event_buyins`` in the same commit, and the two are voided together.

Writes need group admin, resolved the site's way (``assert_group_admin`` with
the MANAGE_GUILD-derived role source). Every write locks its row, writes an
``audit_log`` row with before/after snapshots, and commits once. The tool only
records GP; the clan trades the GP itself in game.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

from quart import Blueprint, jsonify, request
from sqlalchemy import func

from db import AuditLog, Event, EventBuyin, Group, GroupBankEntry, Player, User
from services.group_bank import (
    NAMED_KINDS,
    RECORDABLE_KINDS,
    BankRuleError,
    check_transition,
    clean_text,
    merge_donors,
    signed_amount,
    totals_from_kind_sums,
)
from web_api.common import (
    abort_problem,
    db_session,
    money,
    private_no_store,
    with_cache_headers,
)
from web_api.deps import (
    assert_group_admin,
    current_user_id,
    is_group_admin_role,
    json_body,
    manageable_guild_ids,
    optional_user_id,
    resolve_group_role,
)
from web_api.routes.event_prizes import _UNSET, _clean_proof

group_bank_bp = Blueprint("v1_group_bank", __name__)

# The prize-pot row a transfer writes shows this as its donor.
TRANSFER_DONOR_LABEL = "Clan bank"

_RECENT_LIMIT = 10
_LEDGER_DEFAULT_LIMIT = 50
_LEDGER_MAX_LIMIT = 200
_LEDGER_STATUSES = ("pending", "confirmed", "rejected", "void", "all")


def _rule(fn, *args, **kwargs):
    """Run a services.group_bank rule, turning its error into a problem."""
    try:
        return fn(*args, **kwargs)
    except BankRuleError as e:
        abort_problem(e.status, e.title, e.detail)


def _ts(dt):
    return int(dt.timestamp()) if dt else None


def _is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _snapshot(e: GroupBankEntry) -> str:
    return json.dumps({
        "kind": e.kind,
        "amount": int(e.amount or 0),
        "status": e.status,
        "source": e.source,
        "player_id": e.player_id,
        "rsn": e.rsn,
        "note": e.note,
        "review_note": e.review_note,
        "proof_url": e.proof_url,
        "event_id": e.event_id,
        "event_buyin_id": e.event_buyin_id,
    })


def _audit(s, user_id, group_id, action, entry_id, before, after, event_id=None):
    s.add(AuditLog(
        actor_user_id=user_id,
        group_id=group_id,
        event_id=event_id,
        action=action,
        target=f"group_bank_entries.{entry_id}",
        before=before,
        after=after,
    ))


def _load_group_or_404(s, group_id: int) -> Group:
    group = s.query(Group).filter(Group.group_id == group_id).first()
    if not group:
        abort_problem(404, "Group not found", f"No group {group_id}.")
    return group


def _load_entry_for_update(s, group_id: int, entry_id: int) -> GroupBankEntry:
    row = (
        s.query(GroupBankEntry)
        .filter(GroupBankEntry.id == entry_id, GroupBankEntry.group_id == group_id)
        .with_for_update()
        .first()
    )
    if not row:
        abort_problem(404, "Entry not found", f"No bank entry {entry_id} in this group.")
    return row


def _bank_visibility(s, group_id: int) -> tuple:
    """``(show_on_profile, show_donors)`` from the group's config."""
    from utils import group_config as gc

    def _flag(key: str) -> bool:
        raw = gc.get(s, group_id, key, default=None)
        if raw is None or str(raw).strip() == "":
            return True
        return gc.is_truthy(raw)

    return _flag("bank_show_on_profile"), _flag("bank_show_donors")


def _counterparty(s, body: dict, kind: str, required: bool) -> tuple:
    """``(player_id, rsn, user_id)`` for the other side of the movement."""
    player_id = body.get("player_id")
    if player_id is not None:
        if not _is_id(player_id):
            abort_problem(422, "Invalid player_id", "'player_id' must be an integer or null.")
        player = s.query(Player).filter(Player.player_id == player_id).first()
        if not player:
            abort_problem(404, "Player not found", f"No player {player_id}.")
        return player_id, player.player_name, getattr(player, "user_id", None)
    rsn = _rule(clean_text, body.get("rsn"), "rsn", 24)
    if required and not rsn:
        who = "who donated" if kind == "donation" else "who was paid"
        abort_problem(422, "Name required", f"Provide a 'player_id' or an 'rsn' for {who}.")
    if rsn:
        # A typed name that is a tracked account becomes that account, so
        # donor totals follow the player through a name change.
        player = _match_player(s, rsn)
        if player is not None:
            return player.player_id, player.player_name, getattr(player, "user_id", None)
    return None, rsn, None


def _match_player(s, rsn: str):
    """The tracked account an RSN names, under OSRS name equivalence."""
    from utils.rsn import find_player_by_rsn

    return find_player_by_rsn(s, Player, rsn)


def _entry_row(e: GroupBankEntry, names: dict, events: dict, users: dict, can_manage: bool) -> dict:
    return {
        "id": e.id,
        "kind": e.kind,
        "amount": money(int(e.amount or 0)),
        "status": e.status,
        "source": e.source,
        "player_id": e.player_id,
        # Current name for a tracked account, the stored label otherwise.
        "rsn": names.get(e.player_id) or e.rsn,
        # Notes are a staff affordance; the screenshot rides with the row (it
        # exists to make the numbers verifiable), like the prize pot.
        "note": e.note if can_manage else None,
        "review_note": e.review_note if can_manage else None,
        "proof_url": e.proof_url,
        "event_id": e.event_id,
        "event_name": events.get(e.event_id),
        "submitted_by": users.get(e.user_id) if can_manage else None,
        "created_at": _ts(e.created_at),
        "confirmed_at": _ts(e.confirmed_at),
    }


def _decorate(s, rows, can_manage: bool) -> list:
    """Entry dicts with player names, event names and submitter names resolved
    in three batched lookups."""
    pids = {r.player_id for r in rows if r.player_id is not None}
    eids = {r.event_id for r in rows if r.event_id is not None}
    uids = {r.user_id for r in rows if r.user_id is not None} if can_manage else set()
    names = dict(
        s.query(Player.player_id, Player.player_name).filter(Player.player_id.in_(pids)).all()
    ) if pids else {}
    events = dict(
        s.query(Event.id, Event.name).filter(Event.id.in_(eids)).all()
    ) if eids else {}
    users = dict(
        s.query(User.user_id, User.username).filter(User.user_id.in_(uids)).all()
    ) if uids else {}
    return [_entry_row(r, names, events, users, can_manage) for r in rows]


def _summary(s, group_id: int) -> dict:
    """Totals + donor ranking over confirmed rows, computed in SQL."""
    kind_sums = (
        s.query(GroupBankEntry.kind, func.sum(GroupBankEntry.amount), func.count(GroupBankEntry.id))
        .filter(GroupBankEntry.group_id == group_id, GroupBankEntry.status == "confirmed")
        .group_by(GroupBankEntry.kind)
        .all()
    )
    totals = totals_from_kind_sums(kind_sums)
    confirmed_donation = (
        GroupBankEntry.group_id == group_id,
        GroupBankEntry.status == "confirmed",
        GroupBankEntry.kind == "donation",
    )
    by_player = (
        s.query(GroupBankEntry.player_id, func.sum(GroupBankEntry.amount), func.count(GroupBankEntry.id))
        .filter(*confirmed_donation, GroupBankEntry.player_id.isnot(None))
        .group_by(GroupBankEntry.player_id)
        .all()
    )
    by_name = (
        s.query(GroupBankEntry.rsn, func.sum(GroupBankEntry.amount), func.count(GroupBankEntry.id))
        .filter(*confirmed_donation, GroupBankEntry.player_id.is_(None))
        .group_by(GroupBankEntry.rsn)
        .all()
    )
    pids = [pid for pid, _t, _c in by_player]
    player_names = dict(
        s.query(Player.player_id, Player.player_name).filter(Player.player_id.in_(pids)).all()
    ) if pids else {}
    top, donor_count = merge_donors(by_player, by_name, player_names)
    return {"totals": totals, "top_donors": top, "donor_count": donor_count}


def _pending_count(s, group_id: int) -> int:
    return int(
        s.query(func.count(GroupBankEntry.id))
        .filter(GroupBankEntry.group_id == group_id, GroupBankEntry.status == "pending")
        .scalar() or 0
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@group_bank_bp.get("/groups/<int:group_id>/bank")
async def get_bank(group_id: int):
    """The bank headline for the group page. Staff always get it all; the
    public gets it while ``bank_show_on_profile`` is on, and the donor list
    while ``bank_show_donors`` is on."""
    viewer_id = optional_user_id()
    manage_ids = manageable_guild_ids(viewer_id) if viewer_id is not None else set()

    def _load():
        with db_session() as s:
            _load_group_or_404(s, group_id)
            can_manage = viewer_id is not None and is_group_admin_role(
                resolve_group_role(s, viewer_id, group_id, manage_ids)
            )
            show_on_profile, show_donors = _bank_visibility(s, group_id)
            if not (can_manage or show_on_profile):
                return {"visible": False, "can_manage": False}
            summary = _summary(s, group_id)
            totals = summary["totals"]
            recent_rows = (
                s.query(GroupBankEntry)
                .filter(GroupBankEntry.group_id == group_id, GroupBankEntry.status == "confirmed")
                .order_by(GroupBankEntry.created_at.desc(), GroupBankEntry.id.desc())
                .limit(_RECENT_LIMIT)
                .all()
            )
            donors_visible = can_manage or show_donors
            return {
                "visible": True,
                "show_on_profile": show_on_profile,
                "show_donors": show_donors,
                "balance": money(totals["balance"]),
                "donated_total": money(totals["donated"]),
                "paid_out_total": money(totals["paid_out"]),
                "adjustment_total": money(totals["adjustments"]),
                "donation_count": totals["donation_count"],
                "donor_count": summary["donor_count"],
                "top_donors": [
                    {**d, "total": money(d["total"])} for d in summary["top_donors"]
                ] if donors_visible else None,
                "recent": _decorate(s, recent_rows, can_manage) if donors_visible else None,
                "pending_count": _pending_count(s, group_id) if can_manage else 0,
                "has_activity": bool(totals["by_kind"]),
                "can_manage": can_manage,
            }

    payload = await asyncio.to_thread(_load)
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


@group_bank_bp.get("/groups/<int:group_id>/bank/entries")
async def list_bank_entries(group_id: int):
    """One page of the ledger, newest first, for the admin Bank tab. Also
    carries the pending count and the events a transfer can target."""
    user_id = current_user_id()
    manage_ids = manageable_guild_ids(user_id)
    status = (request.args.get("status") or "all").strip().lower()
    if status not in _LEDGER_STATUSES:
        abort_problem(422, "Invalid status", f"'status' must be one of {list(_LEDGER_STATUSES)}.")
    try:
        limit = int(request.args.get("limit", _LEDGER_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _LEDGER_DEFAULT_LIMIT
    limit = max(1, min(limit, _LEDGER_MAX_LIMIT))
    before_raw = request.args.get("before_id")
    try:
        before_id = int(before_raw) if before_raw not in (None, "") else None
    except (TypeError, ValueError):
        abort_problem(422, "Invalid cursor", "'before_id' must be an integer.")

    def _load():
        with db_session() as s:
            _load_group_or_404(s, group_id)
            assert_group_admin(s, user_id, group_id, manage_ids)
            q = s.query(GroupBankEntry).filter(GroupBankEntry.group_id == group_id)
            if status != "all":
                q = q.filter(GroupBankEntry.status == status)
            if before_id is not None:
                q = q.filter(GroupBankEntry.id < before_id)
            rows = q.order_by(GroupBankEntry.id.desc()).limit(limit + 1).all()
            more = len(rows) > limit
            rows = rows[:limit]
            events = (
                s.query(Event.id, Event.name, Event.status, Event.buyins_enabled, Event.ends_at)
                .filter(Event.group_id == group_id, Event.status.in_(("draft", "active")))
                .order_by(Event.id.desc())
                .limit(50)
                .all()
            )
            now = datetime.now()
            return {
                "entries": _decorate(s, rows, True),
                "next_before_id": rows[-1].id if more and rows else None,
                "pending_count": _pending_count(s, group_id),
                "transfer_events": [
                    {"id": eid, "name": name, "status": st, "pot_enabled": bool(pot)}
                    for eid, name, st, pot, ends_at in events
                    # An active event past its end reads as over (events.py
                    # _effective_status); a transfer into it would be refused.
                    if not (ends_at and ends_at < now)
                ],
            }

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
@group_bank_bp.post("/groups/<int:group_id>/bank/entries")
async def record_bank_entry(group_id: int):
    """Staff record a donation, withdrawal, payout or adjustment. Confirmed
    unless ``status: "pending"`` is asked for."""
    user_id = current_user_id()
    manage_ids = manageable_guild_ids(user_id)
    body = await json_body()

    kind = body.get("kind")
    if kind not in RECORDABLE_KINDS:
        abort_problem(
            422, "Invalid kind",
            f"'kind' must be one of {list(RECORDABLE_KINDS)} (use /bank/transfer to fund an event).",
        )
    amount = _rule(signed_amount, kind, body.get("amount"))
    note = _rule(clean_text, body.get("note"), "note")
    proof = _clean_proof(body)
    status = body.get("status") or "confirmed"
    if status not in ("confirmed", "pending"):
        abort_problem(422, "Invalid status", "'status' must be 'confirmed' or 'pending'.")
    event_id = body.get("event_id")
    if event_id is not None and not _is_id(event_id):
        abort_problem(422, "Invalid event_id", "'event_id' must be an integer or null.")

    def _apply():
        with db_session() as s:
            _load_group_or_404(s, group_id)
            assert_group_admin(s, user_id, group_id, manage_ids)
            if event_id is not None:
                ev = s.query(Event).filter(Event.id == event_id).first()
                if not ev or ev.group_id != group_id:
                    abort_problem(404, "Event not found", f"No event {event_id} in this group.")
            player_id, rsn, uid = _counterparty(s, body, kind, required=kind in NAMED_KINDS)
            now = datetime.utcnow()
            row = GroupBankEntry(
                group_id=group_id,
                kind=kind,
                amount=amount,
                status=status,
                source="staff",
                player_id=player_id,
                rsn=rsn,
                user_id=uid,
                note=note,
                proof_url=None if proof is _UNSET else proof,
                event_id=event_id,
                created_by_user_id=user_id,
                acted_by_user_id=user_id,
                confirmed_at=now if status == "confirmed" else None,
            )
            s.add(row)
            s.flush()
            _audit(s, user_id, group_id, "group.bank.record", row.id, None, _snapshot(row),
                   event_id=event_id)
            s.commit()
            return row.id

    entry_id = await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"id": entry_id}))


@group_bank_bp.patch("/groups/<int:group_id>/bank/entries/<int:entry_id>")
async def update_bank_entry(group_id: int, entry_id: int):
    """Edit an entry, or review it: confirm or reject a pending one, or take a
    decision back (to pending). A transfer's amount and status belong to the
    prize-pot row it wrote, so only its note and screenshot are editable."""
    user_id = current_user_id()
    manage_ids = manageable_guild_ids(user_id)
    body = await json_body()

    def _apply():
        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manage_ids)
            row = _load_entry_for_update(s, group_id, entry_id)
            if row.status == "void":
                abort_problem(409, "Entry removed", "A removed entry can't be edited.")
            if row.kind == "event_transfer" and any(
                k in body for k in ("amount", "status", "player_id", "rsn")
            ):
                abort_problem(
                    409, "Transfers are fixed",
                    "Remove this transfer and make a new one to change it.",
                )
            before = _snapshot(row)
            if "amount" in body:
                row.amount = _rule(signed_amount, row.kind, body.get("amount"))
            if "note" in body:
                row.note = _rule(clean_text, body.get("note"), "note")
            if "review_note" in body:
                row.review_note = _rule(clean_text, body.get("review_note"), "review_note")
            if "player_id" in body or "rsn" in body:
                pid, rsn, uid = _counterparty(s, body, row.kind, required=row.kind in NAMED_KINDS)
                row.player_id, row.rsn = pid, rsn
                if uid is not None:
                    row.user_id = row.user_id or uid
            proof = _clean_proof(body)
            if proof is not _UNSET:
                row.proof_url = proof
            if "status" in body:
                new = body.get("status")
                _rule(check_transition, row.status, new)
                if new == "confirmed" and row.status != "confirmed":
                    row.confirmed_at = datetime.utcnow()
                row.status = new
            row.acted_by_user_id = user_id
            _audit(s, user_id, group_id, "group.bank.update", entry_id, before, _snapshot(row),
                   event_id=row.event_id)
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


@group_bank_bp.delete("/groups/<int:group_id>/bank/entries/<int:entry_id>")
async def delete_bank_entry(group_id: int, entry_id: int):
    """Void an entry that ever counted (kept for audit); hard-delete one that
    never did. Voiding a transfer also voids the prize-pot donation it wrote."""
    user_id = current_user_id()
    manage_ids = manageable_guild_ids(user_id)

    def _apply():
        with db_session() as s:
            assert_group_admin(s, user_id, group_id, manage_ids)
            row = _load_entry_for_update(s, group_id, entry_id)
            if row.status == "void":
                return True, None
            before = _snapshot(row)
            bumped_event = None
            ever_counted = row.status == "confirmed" or row.confirmed_at is not None
            if ever_counted:
                row.status = "void"
                row.acted_by_user_id = user_id
                after = _snapshot(row)
                if row.kind == "event_transfer" and row.event_buyin_id is not None:
                    buyin = (
                        s.query(EventBuyin)
                        .filter(EventBuyin.id == row.event_buyin_id)
                        .with_for_update()
                        .first()
                    )
                    if buyin is not None and buyin.status != "void":
                        buyin_before = json.dumps({"status": buyin.status, "amount": int(buyin.amount or 0)})
                        buyin.status = "void"
                        buyin.acted_by_user_id = user_id
                        s.add(AuditLog(
                            actor_user_id=user_id, group_id=group_id, event_id=buyin.event_id,
                            action="event.buyin.delete",
                            target=f"web_event_buyins.{buyin.id}",
                            before=buyin_before,
                            after=json.dumps({"status": "void", "via": f"group_bank_entries.{entry_id}"}),
                        ))
                        bumped_event = buyin.event_id
            else:
                s.delete(row)
                after = None
            _audit(s, user_id, group_id, "group.bank.delete", entry_id, before, after,
                   event_id=row.event_id)
            s.commit()
            return ever_counted, bumped_event

    voided, bumped_event = await asyncio.to_thread(_apply)
    if bumped_event is not None:
        from web_api.routes.events import _bump

        _bump(bumped_event)
    return private_no_store(jsonify({"ok": True, "voided": voided}))


@group_bank_bp.post("/groups/<int:group_id>/bank/transfer")
async def transfer_to_event(group_id: int):
    """Move GP from the bank into one of this group's event prize pots: a
    confirmed ``event_transfer`` here plus a paid donation in the pot, written
    together."""
    user_id = current_user_id()
    manage_ids = manageable_guild_ids(user_id)
    body = await json_body()

    event_id = body.get("event_id")
    if not _is_id(event_id):
        abort_problem(422, "Invalid event_id", "'event_id' must be an integer.")
    amount = _rule(signed_amount, "event_transfer", body.get("amount"))
    note = _rule(clean_text, body.get("note"), "note")

    def _apply():
        from web_api.event_scope import is_staff_hosted
        from web_api.routes.events import _effective_status

        with db_session() as s:
            _load_group_or_404(s, group_id)
            assert_group_admin(s, user_id, group_id, manage_ids)
            ev = s.query(Event).filter(Event.id == event_id).with_for_update().first()
            if not ev or ev.group_id != group_id or is_staff_hosted(ev):
                abort_problem(404, "Event not found", f"No event {event_id} run by this group.")
            if _effective_status(ev) == "past":
                abort_problem(409, "Event is over", "GP can't be moved into an event that has ended.")
            if not getattr(ev, "buyins_enabled", False):
                abort_problem(
                    422, "Prize pot is off",
                    "Turn on this event's prize pot before moving GP into it.",
                )
            now = datetime.utcnow()
            buyin = EventBuyin(
                event_id=ev.id,
                team_id=None,
                player_id=None,
                rsn=TRANSFER_DONOR_LABEL,
                user_id=None,
                kind="donation",
                amount=-amount,  # signed_amount made the bank side negative
                status="paid",
                note=note,
                acted_by_user_id=user_id,
                paid_at=now,
            )
            s.add(buyin)
            s.flush()
            row = GroupBankEntry(
                group_id=group_id,
                kind="event_transfer",
                amount=amount,
                status="confirmed",
                source="staff",
                note=note,
                event_id=ev.id,
                event_buyin_id=buyin.id,
                created_by_user_id=user_id,
                acted_by_user_id=user_id,
                confirmed_at=now,
            )
            s.add(row)
            s.flush()
            _audit(s, user_id, group_id, "group.bank.transfer", row.id, None, _snapshot(row),
                   event_id=ev.id)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=group_id, event_id=ev.id,
                action="event.buyin.record",
                target=f"web_event_buyins.{buyin.id}",
                before=None,
                after=json.dumps({"kind": "donation", "amount": -amount, "status": "paid",
                                  "via": f"group_bank_entries.{row.id}"}),
            ))
            s.commit()
            return row.id, ev.id

    entry_id, ev_id = await asyncio.to_thread(_apply)
    from web_api.routes.events import _bump

    _bump(ev_id)
    return private_no_store(jsonify({"id": entry_id}))
