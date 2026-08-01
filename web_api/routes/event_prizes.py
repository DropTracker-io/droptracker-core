"""Prize pot — buy-ins & donations (web52a).

The web-facing surface over :class:`db.models.EventBuyin`: a per-event GP
ledger of participant **buy-ins** (a stake to enter, with a paid tick) and
**donations** (extra/standalone GP), summed into an advertised **prize pot**.
The tool tracks/advertises GP only — payouts are traded in-game by the clan
(like split-tracking); nothing here moves real GP or ``EventTeam.score``.

  GET    /api/v1/events/{id}/pot                         -> pot read (public)
  POST   /api/v1/events/{id}/buyins   { player_id?|rsn?, team_id?, kind?,
                                        amount, status?, note?,
                                        proof_key? }                    -> { id }
  PATCH  /api/v1/events/{id}/buyins/{buyinId} { amount?, status?, note?,
                                                proof_key? }            -> { ok }
  DELETE /api/v1/events/{id}/buyins/{buyinId}            -> { ok, voided }

``proof_key`` (web75a) is the object key returned by ``POST /uploads/proof`` —
the shared, Pillow-validated screenshot uploader the manual-submission form
already uses. The URL is built here from ``B2_CDN_BASE_URL``, never taken from
the client, so the ledger can only ever point at our own CDN. Send
``proof_key: null`` on a PATCH to detach.

Writes require event-admin auth (the confirmed ``events`` entitlement); when
``prize_config.allow_leader_mark`` is on, a **team leader** may also
record/tick/void buy-ins scoped to their OWN team. Every write locks the row
(``with_for_update``), writes an ``audit_log`` row, and bumps the event's SSE
channel so the live pot headline refreshes — the ``POST /events/{id}/award``
skeleton, applied to the pot ledger.

A focused route file (not events.py, already 3k+ lines) following the
``event_discord.py`` sub-resource precedent.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from quart import Blueprint, jsonify
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from db import (
    AuditLog,
    Event,
    EventBuyin,
    EventSignup,
    EventTeam,
    EventTeamMember,
    Player,
    EVENT_BUYIN_KINDS,
    EVENT_BUYIN_STATUSES,
)
from web_api.common import (
    abort_problem,
    db_session,
    money,
    private_no_store,
    with_cache_headers,
)
from web_api.deps import current_user_id, json_body, optional_user_id
from web_api.event_prizes import (
    MAX_BUYIN_AMOUNT,
    effective_prize_config,
    pot_line,
    pot_summary,
)
from web_api.routes.events import (
    _assert_event_admin,
    _bump,
    _can_view_restricted,
    _deny_restricted,
    _is_event_admin,
    _is_restricted,
    _load_event_or_404,
    _representative_player_id,
    _ts,
)

event_prizes_bp = Blueprint("v1_event_prizes", __name__)

# Statuses accepted on create / edit (never "void" directly — deletes soft-void).
_SETTABLE_STATUSES = ("pledged", "paid")

# Object keys minted by POST /uploads/proof: dt_uploads/{uuid4 hex}.{ext}.
# Matched strictly (web75a) so a caller can never point a ledger row at an
# arbitrary address — the stored URL is built from B2_CDN_BASE_URL below, not
# taken from the request body.
_PROOF_KEY_RE = re.compile(r"^dt_uploads/[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")

# "field absent" vs. an explicit null (= detach the screenshot).
_UNSET = object()


# --------------------------------------------------------------------------- #
# Authorization: admins always (entitlement-gated); team leaders may tick their
# OWN team's rows when prize_config.allow_leader_mark (decision #2).
# --------------------------------------------------------------------------- #
def _pot_role(s, user_id, ev, team_id=None):
    """``'admin'`` | ``'leader'`` | ``None`` — how this user may touch the pot.

    ``allow_leader_mark`` is advertised as "leaders can tick their team's
    buy-ins paid" — so a leader's write access is limited to exactly that
    (status/note on existing rows, and seeding their own team's checklist at
    the config default). Creating rows with arbitrary amounts, editing
    amounts, and voiding are admin-only (audit: leaders previously had full
    pot CRUD up to the amount cap)."""
    if _is_event_admin(s, user_id, ev):
        # Re-assert to bind the 'events' entitlement (role alone isn't enough).
        _assert_event_admin(s, user_id, ev)
        return "admin"
    cfg = effective_prize_config(getattr(ev, "prize_config", None))
    if cfg["allow_leader_mark"] and team_id is not None:
        from web_api.event_leadership import team_role_for_user

        if team_role_for_user(s, team_id, user_id):
            return "leader"
    return None


def _assert_pot_writer(s, user_id, ev, team_id=None) -> None:
    if _pot_role(s, user_id, ev, team_id) is None:
        abort_problem(
            403, "Forbidden",
            "You must administer this event (or lead this team) to manage the pot.",
        )


def _snapshot(b: EventBuyin) -> str:
    """Before/after JSON for audit rows."""
    return json.dumps({
        "team_id": b.team_id,
        "player_id": b.player_id,
        "rsn": b.rsn,
        "kind": b.kind,
        "amount": int(b.amount or 0),
        "status": b.status,
        "note": b.note,
        "proof_url": b.proof_url,
    })


def _clean_amount(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        abort_problem(422, "Invalid amount", "'amount' must be an integer number of GP.")
    if not (0 <= value < MAX_BUYIN_AMOUNT):
        abort_problem(422, "Invalid amount", f"'amount' must be between 0 and {MAX_BUYIN_AMOUNT - 1}.")
    return value


def _clean_note(body: dict):
    note = body.get("note")
    if note is None:
        return None
    if not isinstance(note, str):
        abort_problem(422, "Invalid note", "'note' must be a string.")
    note = note.strip()
    if len(note) > 255:
        abort_problem(422, "Invalid note", "'note' must be at most 255 characters.")
    return note or None


def _clean_proof(body: dict):
    """``proof_key`` → the CDN URL to store, ``None`` to detach, ``_UNSET``
    when the caller didn't mention it at all."""
    if "proof_key" not in body:
        return _UNSET
    key = body.get("proof_key")
    if key is None:
        return None
    if not isinstance(key, str):
        abort_problem(422, "Invalid proof", "'proof_key' must be a string or null.")
    key = key.strip()
    if not key:
        return None
    if not _PROOF_KEY_RE.match(key):
        abort_problem(
            422, "Invalid proof",
            "'proof_key' must be an object key returned by the proof uploader.",
        )
    from web_api.routes.submissions import B2_CDN_BASE_URL

    return f"{B2_CDN_BASE_URL.rstrip('/')}/{key}"


def _assert_team_in_event(s, event_id: int, team_id) -> None:
    if team_id is None:
        return
    if not isinstance(team_id, int) or isinstance(team_id, bool):
        abort_problem(422, "Invalid team_id", "'team_id' must be an integer or null.")
    ok = s.query(EventTeam.id).filter(
        EventTeam.id == team_id, EventTeam.event_id == event_id
    ).first()
    if not ok:
        abort_problem(404, "Team not found", f"No team {team_id} in this event.")


# --------------------------------------------------------------------------- #
# Read — public pot (respects Event.visibility + show_contributors)
# --------------------------------------------------------------------------- #
def _buyin_row(b: EventBuyin, name, can_manage: bool) -> dict:
    return {
        "id": b.id,
        "player_id": b.player_id,
        "rsn": name or b.rsn,
        "team_id": b.team_id,
        "kind": b.kind,
        "amount": money(int(b.amount or 0)),
        "status": b.status,
        # Notes are an admin affordance — never on public reads.
        "note": b.note if can_manage else None,
        # Proof, unlike the note, rides with the row: it exists to make the
        # advertised pot verifiable, so it is visible to exactly whoever is
        # allowed to see the contribution itself (show_contributors gates the
        # whole list upstream in _pot_payload).
        "proof_url": b.proof_url,
        "created_at": _ts(b.created_at),
    }


def _pot_payload(s, ev, viewer_id) -> dict:
    can_manage = _is_event_admin(s, viewer_id, ev)
    teams = (
        s.query(EventTeam)
        .filter(EventTeam.event_id == ev.id)
        .order_by(EventTeam.score.desc(), EventTeam.id.asc())
        .all()
    )
    team_ids = [t.id for t in teams]
    cfg = effective_prize_config(getattr(ev, "prize_config", None), team_count=len(teams))

    member_counts: dict = {}
    if team_ids:
        for tid, cnt in (
            # EventTeamMember has a composite PK (team_id, player_id) — no id.
            s.query(EventTeamMember.team_id, func.count(EventTeamMember.player_id))
            .filter(EventTeamMember.team_id.in_(team_ids))
            .group_by(EventTeamMember.team_id)
            .all()
        ):
            member_counts[tid] = int(cnt or 0)

    # All live rows (void excluded) once; aggregate + build contributors in
    # Python — a pot is dozens of rows, not thousands.
    rows = (
        s.query(EventBuyin)
        .filter(EventBuyin.event_id == ev.id, EventBuyin.status != "void")
        .order_by(EventBuyin.created_at.asc(), EventBuyin.id.asc())
        .all()
    )
    pids = {r.player_id for r in rows if r.player_id}
    names: dict = {}
    if pids:
        for pid, nm in (
            s.query(Player.player_id, Player.player_name)
            .filter(Player.player_id.in_(pids)).all()
        ):
            names[pid] = nm

    total = buyin_total = donation_total = 0
    per_team_paid: dict = {}  # team_id -> [total, paid_buyin_count]
    # Contributions carrying no team (web71a): buy-ins taken at sign-up before
    # the draft, plus pot-wide donations. Aggregated the same way as a team so
    # sum(per_team) + unassigned == total, and so the pre-draft checklist has a
    # headline of its own.
    unassigned_paid = unassigned_count = 0
    unassigned_members: set = set()
    for r in rows:
        if r.team_id is None and r.kind == "buyin" and r.player_id is not None:
            unassigned_members.add(r.player_id)
        if r.status != "paid":
            continue
        amt = int(r.amount or 0)
        total += amt
        if r.kind == "donation":
            donation_total += amt
        else:
            buyin_total += amt
        if r.team_id is not None:
            pt = per_team_paid.setdefault(r.team_id, [0, 0])
            pt[0] += amt
            if r.kind == "buyin":
                pt[1] += 1
        else:
            unassigned_paid += amt
            if r.kind == "buyin":
                unassigned_count += 1

    per_team = [
        {
            "team_id": t.id,
            "name": t.name,
            "total": money(per_team_paid.get(t.id, [0, 0])[0]),
            "paid_count": per_team_paid.get(t.id, [0, 0])[1],
            "member_count": member_counts.get(t.id, 0),
        }
        for t in teams
    ]

    # Contributor list: admins see every live row (to tick pledged ones);
    # the public sees paid rows only, and only when show_contributors is on.
    contributors = None
    if can_manage or cfg["show_contributors"]:
        visible = rows if can_manage else [r for r in rows if r.status == "paid"]
        contributors = [_buyin_row(r, names.get(r.player_id), can_manage) for r in visible]

    return {
        "enabled": bool(getattr(ev, "buyins_enabled", False)),
        "total": money(total),
        "buyin_total": money(buyin_total),
        "donation_total": money(donation_total),
        "config": {
            "default_buyin": money(cfg["default_buyin"]),
            "distribution": cfg["distribution"],
            "top_n": cfg["top_n"],
            "splits": cfg["splits"],
            "advertise": cfg["advertise"],
            "show_contributors": cfg["show_contributors"],
            "allow_leader_mark": cfg["allow_leader_mark"],
        },
        "per_team": per_team,
        "unassigned": {
            "total": money(unassigned_paid),
            "paid_count": unassigned_count,
            "member_count": len(unassigned_members),
        },
        "contributors": contributors,
        "can_manage": can_manage,
    }


@event_prizes_bp.get("/events/<int:event_id>/pot")
async def get_pot(event_id: int):
    viewer_id = optional_user_id()

    def _load():
        with db_session() as s:
            ev = s.query(Event).filter(Event.id == event_id).first()
            if not ev:
                return None
            # Restricted events (draft / private): reasoned 403 for signed-in
            # outsiders, anonymized 404 for anonymous — exactly like the event
            # detail read.
            if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
                _deny_restricted(ev, viewer_id)
                return None
            return _pot_payload(s, ev, viewer_id)

    payload = await asyncio.to_thread(_load)
    if payload is None:
        abort_problem(404, "Event not found", f"No event {event_id}.")
    if viewer_id is not None:
        # Viewer-specific (can_manage, admin-only rows/notes) — never shared.
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


# --------------------------------------------------------------------------- #
# Writes — record / edit / delete
# --------------------------------------------------------------------------- #
@event_prizes_bp.post("/events/<int:event_id>/buyins")
async def record_buyin(event_id: int):
    """Record a buy-in or donation. Buy-ins default ``pledged`` (the roster
    checklist ticks them ``paid`` later); donations default ``paid``."""
    user_id = current_user_id()
    body = await json_body()

    kind = body.get("kind") or "buyin"
    if kind not in EVENT_BUYIN_KINDS:
        abort_problem(422, "Invalid kind", f"'kind' must be one of {list(EVENT_BUYIN_KINDS)}.")
    amount = _clean_amount(body.get("amount", 0))
    note = _clean_note(body)
    proof = _clean_proof(body)
    player_id = body.get("player_id")
    if player_id is not None and (not isinstance(player_id, int) or isinstance(player_id, bool)):
        abort_problem(422, "Invalid player_id", "'player_id' must be an integer or null.")
    team_id = body.get("team_id")
    rsn_in = body.get("rsn")
    if rsn_in is not None and not isinstance(rsn_in, str):
        abort_problem(422, "Invalid rsn", "'rsn' must be a string or null.")
    status = body.get("status") or ("paid" if kind == "donation" else "pledged")
    if status not in _SETTABLE_STATUSES:
        abort_problem(422, "Invalid status", f"'status' must be one of {list(_SETTABLE_STATUSES)}.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_team_in_event(s, event_id, team_id)
            # Admin-only: this route takes an arbitrary amount/kind/status —
            # leaders' allow_leader_mark covers ticking and seeding, not
            # authoring contributions (audit).
            _assert_event_admin(s, user_id, ev)

            rsn = None
            uid = None
            if player_id is not None:
                player = s.query(Player).filter(Player.player_id == player_id).first()
                if not player:
                    abort_problem(404, "Player not found", f"No player {player_id}.")
                rsn = player.player_name           # display snapshot
                uid = getattr(player, "user_id", None)
            else:
                # External donor: a free-text label is required to display them.
                rsn = (rsn_in or "").strip()
                if not rsn:
                    abort_problem(
                        422, "Donor required",
                        "Provide a 'player_id' or a free-text 'rsn' for this contribution.",
                    )
                if len(rsn) > 24:
                    abort_problem(422, "Invalid rsn", "'rsn' must be at most 24 characters.")

            row = EventBuyin(
                event_id=event_id,
                team_id=team_id,
                player_id=player_id,
                rsn=rsn,
                user_id=uid,
                kind=kind,
                amount=amount,
                status=status,
                note=note,
                proof_url=None if proof is _UNSET else proof,
                acted_by_user_id=user_id,
                paid_at=datetime.utcnow() if status == "paid" else None,
            )
            s.add(row)
            s.flush()
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                event_id=ev.id,
                action="event.buyin.record",
                target=f"web_event_buyins.{row.id}",
                before=None,
                after=_snapshot(row),
            ))
            s.commit()
            return row.id

    buyin_id = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"id": buyin_id}))


@event_prizes_bp.patch("/events/<int:event_id>/buyins/<int:buyin_id>")
async def update_buyin(event_id: int, buyin_id: int):
    """Edit amount / status / note / proof. Setting ``status='paid'`` stamps
    ``paid_at`` — this is the roster "tick"."""
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            row = (
                s.query(EventBuyin)
                .filter(EventBuyin.id == buyin_id, EventBuyin.event_id == event_id)
                .with_for_update()
                .first()
            )
            if not row:
                abort_problem(404, "Buy-in not found", f"No buy-in {buyin_id} in this event.")
            role = _pot_role(s, user_id, ev, row.team_id)
            if role is None:
                abort_problem(
                    403, "Forbidden",
                    "You must administer this event (or lead this team) to "
                    "manage the pot.",
                )
            if role == "leader" and "amount" in body:
                abort_problem(
                    403, "Leaders mark payments only",
                    "Team leaders can tick contributions paid or pledged (and "
                    "add a note) — amounts are set by event admins.",
                )
            # Voiding takes money OUT of the advertised pot, which this module
            # treats as admin-only everywhere else (see _pot_role's docstring
            # and the DELETE route). A leader may only move a row between
            # pledged and paid — never void one, never resurrect a voided one.
            if role == "leader" and (
                body.get("status") == "void" or row.status == "void"
            ):
                abort_problem(
                    403, "Voiding is an admin action",
                    "Team leaders can tick contributions paid or pledged — "
                    "voiding a contribution (or restoring a voided one) "
                    "changes the advertised pot and is done by event admins.",
                )
            before = _snapshot(row)
            if "amount" in body:
                row.amount = _clean_amount(body.get("amount"))
            if "note" in body:
                row.note = _clean_note(body)
            # Attaching/detaching the screenshot is part of "mark this paid",
            # so it stays inside a leader's allow_leader_mark scope — unlike
            # the amount, which only an event admin sets.
            proof = _clean_proof(body)
            if proof is not _UNSET:
                row.proof_url = proof
            if "status" in body:
                status = body.get("status")
                if status not in EVENT_BUYIN_STATUSES:
                    abort_problem(
                        422, "Invalid status",
                        f"'status' must be one of {list(EVENT_BUYIN_STATUSES)}.",
                    )
                if status == "paid" and row.status != "paid":
                    row.paid_at = datetime.utcnow()
                elif status != "paid":
                    row.paid_at = None
                row.status = status
            row.acted_by_user_id = user_id
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                event_id=ev.id,
                action="event.buyin.update",
                target=f"web_event_buyins.{buyin_id}",
                before=before,
                after=_snapshot(row),
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True}))


@event_prizes_bp.delete("/events/<int:event_id>/buyins/<int:buyin_id>")
async def delete_buyin(event_id: int, buyin_id: int):
    """Soft-void a row that was ever paid (kept for audit / to restore the pot
    on re-enable); hard-delete one that never counted."""
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            row = (
                s.query(EventBuyin)
                .filter(EventBuyin.id == buyin_id, EventBuyin.event_id == event_id)
                .with_for_update()
                .first()
            )
            if not row:
                abort_problem(404, "Buy-in not found", f"No buy-in {buyin_id} in this event.")
            # Admin-only: voiding/deleting removes money from the advertised
            # pot — outside the "tick paid" scope leaders are granted.
            _assert_event_admin(s, user_id, ev)
            before = _snapshot(row)
            ever_paid = row.status == "paid" or row.paid_at is not None
            if ever_paid:
                row.status = "void"
                row.acted_by_user_id = user_id
                after = _snapshot(row)
            else:
                s.delete(row)
                after = None
            s.add(AuditLog(
                actor_user_id=user_id,
                group_id=ev.group_id,
                event_id=ev.id,
                action="event.buyin.delete",
                target=f"web_event_buyins.{buyin_id}",
                before=before,
                after=after,
            ))
            s.commit()
            return ever_paid

    voided = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"ok": True, "voided": voided}))


# --------------------------------------------------------------------------- #
# Bulk seed + manual announce (P1)
# --------------------------------------------------------------------------- #
@event_prizes_bp.post("/events/<int:event_id>/buyins/bulk")
async def bulk_seed_buyins(event_id: int):
    """Seed one ``pledged`` buy-in row per participant at ``default_buyin`` —
    a ready-to-tick checklist. Optionally scoped to one ``team_id``; unscoped,
    it also covers **sign-ups that no draft has placed yet** (web71a), whose
    rows are seeded with ``team_id = NULL`` and follow their player onto a team
    when the draft lands. Anyone who already has a (non-void) buy-in row is
    skipped, so re-running is safe."""
    user_id = current_user_id()
    body = await json_body(required=False) or {}
    team_id = body.get("team_id")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_team_in_event(s, event_id, team_id)
            _assert_pot_writer(s, user_id, ev, team_id)
            cfg = effective_prize_config(getattr(ev, "prize_config", None))
            default = int(cfg["default_buyin"])
            # Serialize concurrent seeds on the event row (audit): the
            # existing-rows check below is check-then-insert, and MariaDB
            # can't express "unique unless void" — two simultaneous seeds
            # would both read the same pre-insert set and double-pledge every
            # member; both later ticked paid = the pot counts twice.
            s.query(Event.id).filter(Event.id == event_id) \
                .with_for_update().first()
            members_q = (
                s.query(EventTeamMember.team_id, EventTeamMember.player_id,
                        Player.player_name, Player.user_id)
                .join(Player, Player.player_id == EventTeamMember.player_id)
                .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                .filter(EventTeam.event_id == event_id)
            )
            if team_id is not None:
                members_q = members_q.filter(EventTeamMember.team_id == team_id)
            targets = list(members_q.all())
            if team_id is None:
                # Unscoped seed: everyone who signed up but is still in the
                # pool gets a team-less row, so a pre-draft event can collect
                # buy-ins without inventing a placeholder team. Scoped to one
                # team, only that roster is touched (unchanged behaviour).
                placed = {pid for (_tid, pid, _n, _u) in targets}
                targets += [
                    (None, pid, pname, puid)
                    for pid, pname, puid in (
                        s.query(EventSignup.player_id, Player.player_name,
                                Player.user_id)
                        .join(Player, Player.player_id == EventSignup.player_id)
                        .filter(EventSignup.event_id == event_id)
                        .all()
                    )
                    if pid not in placed
                ]
            existing = {
                p for (p,) in
                # Locking read = current read: a seed that waited on the
                # event-row lock must see the winner's committed rows, not
                # its own pre-lock snapshot.
                #
                # Keyed on player alone, NOT (team, player): a player's buy-in
                # is one contribution to one event, and its team_id moves with
                # them (web71a). Keying on the pair would re-seed a second row
                # every time the draft moved someone.
                s.query(EventBuyin.player_id)
                .filter(EventBuyin.event_id == event_id,
                        EventBuyin.kind == "buyin",
                        EventBuyin.status != "void")
                .with_for_update().all()
            }
            created = 0
            for tid, pid, pname, puid in targets:
                if pid in existing:
                    continue
                existing.add(pid)   # a pooled sign-up may also hold a placement
                s.add(EventBuyin(
                    event_id=event_id, team_id=tid, player_id=pid, rsn=pname,
                    user_id=puid, kind="buyin", amount=default, status="pledged",
                    acted_by_user_id=user_id,
                ))
                created += 1
            if created:
                s.add(AuditLog(
                    actor_user_id=user_id, group_id=ev.group_id,
                    event_id=ev.id,
                    action="event.buyin.bulk_seed",
                    target=f"web_events.{event_id}",
                    before=None,
                    after=json.dumps({"created": created, "team_id": team_id,
                                      "default": default}),
                ))
                s.commit()
            return created

    created = await asyncio.to_thread(_apply)
    _bump(event_id)
    return private_no_store(jsonify({"created": created}))


@event_prizes_bp.post("/events/<int:event_id>/pot/announce")
async def announce_pot(event_id: int):
    """Post the current pot to the event's Discord announcements channel now —
    an explicit admin action (deliberately manual; buy-in changes never
    auto-post). Enqueues an ``event_pot`` notification the core bot renders."""
    user_id = current_user_id()

    def _apply():
        from datetime import datetime as _dt
        from db.models import EventChannel, NotificationQueue

        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_event_admin(s, user_id, ev)
            if not getattr(ev, "buyins_enabled", False):
                abort_problem(422, "Pot disabled",
                              "Enable the prize pot before announcing it.")
            channel = (
                s.query(EventChannel)
                .filter(EventChannel.event_id == event_id,
                        EventChannel.kind == "announcements")
                .first()
            )
            if not channel:
                abort_problem(422, "No Discord channel",
                              "Configure the event's announcements channel first "
                              "(Event → Discord).")
            rep = _representative_player_id(s, event_id)
            if rep is None:
                abort_problem(422, "No players",
                              "There are no players to route the post through yet.")
            team_count = (
                s.query(EventTeam.id).filter(EventTeam.event_id == event_id).count()
            )
            pot = pot_summary(s, ev, team_count=team_count)
            # money(...)["value_formatted"] gives the same K/M/B abbreviation as
            # the bot's format_gp without importing the (test-stubbed) services
            # package into this route module.
            line = pot_line(
                money(pot["total"])["value_formatted"], pot["distribution"], pot["top_n"],
            )
            payload = {
                "event_id": event_id,
                "event_name": ev.name,
                "pot_announce_line": line,
                # Nonce so repeated announces don't collide on the queue's
                # unique (type, player, group, data) index.
                "posted_at": int(_dt.now().timestamp()),
            }
            try:
                # Same-second double click builds a byte-identical payload —
                # the queue's unique index rejects it. That's a duplicate of
                # an announcement that IS queued, not an error (audit: this
                # used to surface as a raw 500).
                with s.begin_nested():
                    s.add(NotificationQueue(
                        notification_type="event_pot",
                        player_id=rep,
                        group_id=ev.group_id,
                        data=json.dumps(payload),
                        status="pending",
                    ))
                    s.flush()
            except IntegrityError:
                pass
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.pot.announce",
                target=f"web_events.{event_id}", before=None, after="posted",
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
