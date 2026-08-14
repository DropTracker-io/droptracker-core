"""Clan-vs-clan participant roster (Implementation Plan B, Phase 1).

Invite / accept / decline flow for ``web_event_groups``:

  POST   /api/v1/events/{id}/participants                 { group_id } — host
         admin invites an opponent clan (events entitlement enforced on the
         host, exactly like event creation; the opponent never needs a tier).
  GET    /api/v1/events/{id}/participants                 — event admin: the
         roster with group names, roles and invite statuses.
  GET    /api/v1/events/invitations                       — signed-in user's
         invitation inbox: pending invites for clans they administer.
  POST   /api/v1/events/{id}/participants/{gid}/accept    — admin of THAT clan.
  POST   /api/v1/events/{id}/participants/{gid}/decline   — admin of that clan.
  DELETE /api/v1/events/{id}/participants/{gid}           — host admin removes
         a non-host participant (blocked while the clan still has teams).

Standard/global events never have rows here; every write is 422-gated on
``mode == "clan_vs_clan"``. All mutations are audit-logged.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from quart import Blueprint, jsonify

from db import (
    AuditLog,
    Event,
    EventGroup,
    EventTeam,
    EventTeamMember,
    Group,
    GroupAdmin,
    Player,
)
from web_api.common import (
    ProblemException,
    abort_problem,
    db_session,
    private_no_store,
)
from web_api.deps import (
    current_user_id,
    event_manager_group_ids,
    is_event_manager,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
    resolve_group_role,
)
from web_api.routes.events import (
    _assert_event_admin,
    _effective_status,
    _load_event_or_404,
    _summary,
    _sync_event_guilds,
    _ts,
)

event_participants_bp = Blueprint("v1_event_participants", __name__)


# --------------------------------------------------------------------------- #
# Local auth helpers
# --------------------------------------------------------------------------- #
def _assert_admin_of_group(s, user_id: int, group_id: int) -> None:
    """Owner/admin of ``group_id``, an event manager on it, or a superadmin —
    with no entitlement check: responding to an invite must never require the
    invited clan to pay.

    Event managers are included (web96a) because web64a already trusts them to
    fully manage the group's events; leaving them able to run a clan battle but
    not to answer the challenge that starts one was incoherent. It also matches
    ``_assert_event_admin``'s clan_vs_clan branch, so the same people can
    accept an invite and then co-manage the event.
    """
    user = load_user(s, user_id)
    if is_superadmin(user):
        return
    role = resolve_group_role(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
    if role in ("owner", "admin"):
        return
    if is_event_manager(s, user_id, group_id):
        return
    abort_problem(403, "Forbidden", "You must be an admin of that clan.")


def _threads_for_participant_rows(s, event_group_ids: list[int]) -> dict:
    """``{web_event_groups.id: ChatThread}`` for a batch of participant rows.

    Rows invited before web96a have no thread; they simply come back absent and
    the caller renders the invitation without a conversation link (opening the
    page creates one).
    """
    if not event_group_ids:
        return {}
    from db.models import ChatThread
    from services.event_invites import SUBJECT_TYPE, THREAD_KIND

    rows = (
        s.query(ChatThread)
        .filter(
            ChatThread.kind == THREAD_KIND,
            ChatThread.subject_type == SUBJECT_TYPE,
            ChatThread.subject_id.in_([int(i) for i in event_group_ids]),
        )
        .all()
    )
    return {int(t.subject_id): t for t in rows}


def _unread_for_threads(s, thread_ids: list[int], user_id: int) -> dict:
    if not thread_ids:
        return {}
    from services.chat import unread_counts

    return unread_counts(s, thread_ids, user_id)


def _group_name(s, group_id) -> str | None:
    """Clan display name. Used only by the thread endpoint below — the
    invite/accept/remove paths let ``services.event_invites`` resolve names
    itself, so notification lookups stay inside its best-effort boundary and
    never add a query to the path that writes the roster."""
    if not group_id:
        return None
    row = s.query(Group.group_name).filter(Group.group_id == group_id).first()
    return row[0] if row else None


def _require_clan_vs_clan(ev: Event) -> None:
    if (getattr(ev, "mode", None) or "standard") != "clan_vs_clan":
        abort_problem(422, "Not a clan-vs-clan event",
                      "Participants only exist on clan-vs-clan events.")


def _assert_event_not_over(ev: Event) -> None:
    if _effective_status(ev) == "past":
        abort_problem(409, "Event is over", "The participant roster can no longer change.")


def _admin_group_ids(s, user_id: int) -> set[int] | None:
    """Group ids the caller administers (union of memberships, web grants and
    MANAGE_GUILD guilds, filtered by resolved role — same derivation as
    ``GET /me``). ``None`` means superadmin: every group."""
    user = load_user(s, user_id)
    if is_superadmin(user):
        return None
    mgids = manageable_guild_ids(user_id)
    candidates: set[int] = set()
    if user:
        candidates |= {g.group_id for g in user.groups}
    candidates |= {
        gid for (gid,) in
        s.query(GroupAdmin.group_id).filter(GroupAdmin.user_id == user_id).all()
    }
    if mgids:
        candidates |= {
            gid for (gid,) in
            s.query(Group.group_id).filter(Group.guild_id.in_(mgids)).all()
        }
    out = {
        gid for gid in candidates
        if resolve_group_role(s, user_id, gid, mgids, user=user) in ("owner", "admin")
    }
    # web96a: event managers may answer a challenge (see _assert_admin_of_group),
    # so the inbox that surfaces those challenges has to list them too — they
    # hold no group-admin right and would be filtered out by the role check.
    out |= {int(gid) for gid in event_manager_group_ids(s, user_id)}
    return out


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@event_participants_bp.post("/events/<int:event_id>/participants")
async def invite_participant(event_id: int):
    """Host admin invites an opponent clan (409 if already on the roster —
    including the host itself, seeded at create time)."""
    user_id = current_user_id()
    body = await json_body()
    group_id = body.get("group_id")
    if not isinstance(group_id, int):
        abort_problem(422, "Invalid group_id", "'group_id' must be an integer.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _require_clan_vs_clan(ev)
            _assert_event_not_over(ev)
            # Host admin + events entitlement — the host "pays", opponents don't.
            _assert_event_admin(s, user_id, ev.group_id)
            group = s.query(Group).filter(Group.group_id == group_id).first()
            if not group:
                abort_problem(404, "Group not found", f"No group {group_id}.")
            existing = (
                s.query(EventGroup)
                .filter(EventGroup.event_id == event_id, EventGroup.group_id == group_id)
                .first()
            )
            if existing:
                abort_problem(409, "Already on the roster",
                              f"That clan is already {existing.status} on this event.")
            row = EventGroup(
                event_id=event_id, group_id=group_id, role="opponent",
                status="invited", invited_by_user_id=user_id,
            )
            s.add(row)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.participant.invite",
                target=f"web_events.{event_id}.group.{group_id}",
                before=None, after="invited",
            ))
            s.commit()

            # web96a: open the negotiation thread and DM the invited clan's
            # admins. After the commit and best-effort by contract — the
            # invitation itself has already succeeded, and a Discord or Redis
            # hiccup must not turn that into a 500 the host would retry.
            from services.event_invites import announce_invite

            announce_invite(
                s, event=ev, event_group=row,
                # Already loaded, so no extra query — but read defensively:
                # every argument here is evaluated OUTSIDE the notifier's
                # best-effort boundary and must not be able to raise.
                invited_group_name=getattr(group, "group_name", None),
                actor_user_id=user_id,
            )

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


# Cap one bulk-invite request — enough for the 12+-clan case with headroom,
# small enough to bound the roster/existence queries.
MAX_BULK_INVITE = 30


@event_participants_bp.post("/events/<int:event_id>/participants/bulk")
async def invite_participants_bulk(event_id: int):
    """Host admin invites SEVERAL opponent clans in one call (the many-clan
    case). Body ``{"group_ids": [int, ...]}``. Idempotent per clan: a clan
    already on the roster (any status), the host itself, or a group that does
    not exist is returned in ``skipped`` with a reason; freshly-invited clans
    come back in ``invited``. One summary audit row for the batch."""
    user_id = current_user_id()
    body = await json_body()
    raw = body.get("group_ids")
    if not isinstance(raw, list) or not raw:
        abort_problem(422, "Invalid group_ids", "'group_ids' must be a non-empty list.")
    # Dedupe, keep request order, validate ints.
    requested: list[int] = []
    seen: set[int] = set()
    for gid in raw:
        if not isinstance(gid, int):
            abort_problem(422, "Invalid group_ids", "'group_ids' must contain only integers.")
        if gid not in seen:
            seen.add(gid)
            requested.append(gid)
    if len(requested) > MAX_BULK_INVITE:
        abort_problem(422, "Too many clans",
                      f"At most {MAX_BULK_INVITE} clans per invite.")

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _require_clan_vs_clan(ev)
            _assert_event_not_over(ev)
            # Host admin + events entitlement — the host "pays", opponents don't.
            _assert_event_admin(s, user_id, ev.group_id)

            existing = {
                gid for (gid,) in
                s.query(EventGroup.group_id)
                .filter(EventGroup.event_id == event_id,
                        EventGroup.group_id.in_(requested))
                .all()
            }
            names = dict(
                s.query(Group.group_id, Group.group_name)
                .filter(Group.group_id.in_(requested))
                .all()
            )
            invited: list[dict] = []
            skipped: list[dict] = []
            new_rows: list[EventGroup] = []
            for gid in requested:
                if gid == ev.group_id:
                    skipped.append({"group_id": gid, "group_name": names.get(gid),
                                    "reason": "That clan is the host of this event."})
                    continue
                if gid not in names:
                    skipped.append({"group_id": gid, "group_name": None,
                                    "reason": "No such clan."})
                    continue
                if gid in existing:
                    skipped.append({"group_id": gid, "group_name": names.get(gid),
                                    "reason": "Already on the roster."})
                    continue
                row = EventGroup(
                    event_id=event_id, group_id=gid, role="opponent",
                    status="invited", invited_by_user_id=user_id,
                )
                s.add(row)
                new_rows.append(row)
                invited.append({"group_id": gid, "group_name": names.get(gid)})
            if invited:
                s.add(AuditLog(
                    actor_user_id=user_id, group_id=ev.group_id,
                    event_id=ev.id,
                    action="event.participant.invite_bulk",
                    target=f"web_events.{event_id}",
                    before=None, after=f"invited:{len(invited)}",
                ))
                s.commit()

                # web96a: one thread + DM fan-out PER invited clan, so each
                # challenge stays a private conversation between the host and
                # that clan rather than a room every rival can read.
                from services.event_invites import announce_invite

                for row in new_rows:
                    announce_invite(
                        s, event=ev, event_group=row,
                        invited_group_name=names.get(int(row.group_id)),
                        actor_user_id=user_id,
                    )
            return {"invited": invited, "skipped": skipped}

    result = await asyncio.to_thread(_apply)
    return private_no_store(jsonify(result))


@event_participants_bp.get("/events/<int:event_id>/participants")
async def list_participants(event_id: int):
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _require_clan_vs_clan(ev)
            _assert_event_admin(s, user_id, ev)
            rows = (
                s.query(EventGroup, Group.group_name)
                .join(Group, Group.group_id == EventGroup.group_id)
                .filter(EventGroup.event_id == event_id)
                .order_by(EventGroup.created_at.asc())
                .all()
            )
            # web96a: the per-clan negotiation thread, so the host's roster
            # panel can chase a pending invite instead of just staring at it.
            threads = _threads_for_participant_rows(s, [r.id for r, _ in rows])
            unread = _unread_for_threads(s, [t.id for t in threads.values()], user_id)
            return [
                {
                    "group_id": r.group_id,
                    "group_name": name,
                    "role": r.role,
                    "status": r.status,
                    "invited_at": _ts(r.created_at),
                    "responded_at": _ts(r.responded_at),
                    "thread_id": (
                        int(threads[int(r.id)].id) if int(r.id) in threads else None
                    ),
                    "unread": (
                        unread.get(int(threads[int(r.id)].id), 0)
                        if int(r.id) in threads
                        else 0
                    ),
                }
                for r, name in rows
            ]

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@event_participants_bp.get("/events/<int:event_id>/participants/<int:group_id>/thread")
async def participant_thread(event_id: int, group_id: int):
    """The negotiation thread between the host and one invited clan (web96a).

    Get-or-create, so it resolves for invitations sent before this feature
    existed and for clans that have already accepted (the conversation
    continues past the answer — that is the point of it).

    Readable by an admin of THAT clan or by the event's admins; the thread's
    own membership check is what gates the messages themselves.
    """
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _require_clan_vs_clan(ev)
            row = (
                s.query(EventGroup)
                .filter(EventGroup.event_id == event_id,
                        EventGroup.group_id == group_id)
                .first()
            )
            if not row:
                abort_problem(404, "Not a participant",
                              "That clan is not on this event.")
            if row.role == "host":
                # Threads pair the host with ONE invited clan. The host row has
                # no counterpart, so asking for "the host's thread" is always a
                # caller mistake — creating one would make a room the host is
                # alone in.
                abort_problem(
                    422, "No conversation there",
                    "Pick an invited clan — the host has a separate "
                    "conversation with each of them.",
                )
            # Either side may open it: the invited clan's admins, or anyone who
            # administers the event (which for clan_vs_clan already means an
            # admin of some accepted participating clan).
            try:
                _assert_admin_of_group(s, user_id, group_id)
            except ProblemException:
                # Not this clan's admin — the event's own admins may still see
                # it. Anything that isn't an authorization refusal propagates.
                _assert_event_admin(s, user_id, ev)

            from services.chat import resolve_membership, thread_payload
            from services.event_invites import ensure_thread

            thread = ensure_thread(
                s, event=ev, event_group=row,
                host_group_name=_group_name(s, ev.group_id),
                invited_group_name=_group_name(s, group_id),
                created_by_user_id=user_id,
            )
            from db.models import ChatParticipant

            participants = (
                s.query(ChatParticipant)
                .filter(ChatParticipant.thread_id == thread.id)
                .order_by(ChatParticipant.id.asc())
                .all()
            )
            names = {}
            for gid, gname in (
                s.query(Group.group_id, Group.group_name)
                .filter(Group.group_id.in_(
                    [int(p.party_id) for p in participants
                     if p.party_type == "group"] or [0]
                ))
                .all()
            ):
                names[("group", int(gid))] = gname
            payload = thread_payload(
                thread,
                participants=participants,
                unread=_unread_for_threads(s, [thread.id], user_id).get(
                    int(thread.id), 0
                ),
                membership=resolve_membership(s, thread, user_id),
                party_names=names,
            )
            payload["participant_status"] = row.status
            payload["participant_role"] = row.role
            return payload

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@event_participants_bp.get("/events/invitations")
async def list_invitations():
    """Invitation inbox: pending (``invited``) rows on live events, for every
    clan the caller administers."""
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            admin_gids = _admin_group_ids(s, user_id)
            if admin_gids is not None and not admin_gids:
                return []
            q = (
                s.query(EventGroup, Event, Group.group_name)
                .join(Event, Event.id == EventGroup.event_id)
                .join(Group, Group.group_id == EventGroup.group_id)
                .filter(EventGroup.status == "invited")
            )
            if admin_gids is not None:
                q = q.filter(EventGroup.group_id.in_(admin_gids))
            rows = q.order_by(EventGroup.created_at.desc()).all()

            host_names = {}
            host_ids = {ev.group_id for _, ev, _ in rows if ev.group_id}
            if host_ids:
                host_names = dict(
                    s.query(Group.group_id, Group.group_name)
                    .filter(Group.group_id.in_(host_ids))
                    .all()
                )
            live = [(r, ev, gname) for r, ev, gname in rows
                    if _effective_status(ev) != "past"]  # expired = nothing actionable

            # web96a: the negotiation thread for each invitation, so the inbox
            # can link straight into it and show an unread pill. Two batched
            # queries for the whole list, not one per invite.
            threads = _threads_for_participant_rows(s, [r.id for r, _, _ in live])
            unread = _unread_for_threads(s, [t.id for t in threads.values()], user_id)

            out = []
            for r, ev, gname in live:
                thread = threads.get(int(r.id))
                out.append({
                    "event": _summary(ev),
                    "group_id": r.group_id,
                    "group_name": gname,
                    "host_group_name": host_names.get(ev.group_id),
                    "invited_at": _ts(r.created_at),
                    "thread_id": int(thread.id) if thread is not None else None,
                    "unread": unread.get(int(thread.id), 0) if thread is not None else 0,
                })
            return out

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


@event_participants_bp.get("/events/recruiting")
async def list_recruiting():
    """Events the caller's clans are recruiting for (Phase 3 opt-in banner):
    clan-vs-clan events open to member opt-in (``formation_mode`` is not
    admin_assign), where one of the caller's clans is an accepted participant
    and none of the caller's players is on a team yet."""
    user_id = current_user_id()

    def _load():
        with db_session() as s:
            user = load_user(s, user_id)
            if not user:
                return []
            my_gids = {g.group_id for g in user.groups}
            if not my_gids:
                return []
            rows = (
                s.query(EventGroup, Event, Group.group_name)
                .join(Event, Event.id == EventGroup.event_id)
                .join(Group, Group.group_id == EventGroup.group_id)
                .filter(
                    EventGroup.status == "accepted",
                    EventGroup.group_id.in_(my_gids),
                    Event.mode == "clan_vs_clan",
                    Event.formation_mode != "admin_assign",
                    Event.status.in_(("draft", "active")),
                )
                .order_by(Event.id.desc())
                .all()
            )
            if not rows:
                return []
            my_pids = {
                pid for (pid,) in
                s.query(Player.player_id).filter(Player.user_id == user_id).all()
            }
            out = []
            seen = set()
            for r, ev, gname in rows:
                if ev.id in seen or _effective_status(ev) == "past":
                    continue
                seen.add(ev.id)
                if my_pids:
                    joined = (
                        s.query(EventTeamMember.player_id)
                        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
                        .filter(
                            EventTeam.event_id == ev.id,
                            EventTeamMember.player_id.in_(my_pids),
                        )
                        .first()
                    )
                    if joined:
                        continue
                out.append({
                    "event": _summary(ev),
                    "group_id": r.group_id,
                    "group_name": gname,
                })
            return out

    return private_no_store(jsonify(await asyncio.to_thread(_load)))


def _respond_to_invitation(event_id: int, group_id: int, user_id: int, accept: bool,
                           mirror_discord_event: bool = False) -> None:
    with db_session() as s:
        ev = _load_event_or_404(s, event_id)
        _require_clan_vs_clan(ev)
        _assert_event_not_over(ev)
        _assert_admin_of_group(s, user_id, group_id)
        row = (
            s.query(EventGroup)
            .filter(EventGroup.event_id == event_id, EventGroup.group_id == group_id)
            .first()
        )
        if not row:
            abort_problem(404, "Not invited", "That clan has no invitation to this event.")
        if row.status != "invited":
            abort_problem(409, "Already responded",
                          f"That invitation is already {row.status}.")
        row.status = "accepted" if accept else "declined"
        row.responded_at = datetime.now()
        if accept:
            # Opt-in only (accept-time checkbox): mirror the Discord scheduled
            # event into this clan's own linked guild. Never on by default —
            # accepting must not create anything in the accepting clan's
            # server unasked.
            row.mirror_discord_event = bool(mirror_discord_event)
        s.add(AuditLog(
            actor_user_id=user_id, group_id=group_id, event_id=event_id,
            action=f"event.participant.{'accept' if accept else 'decline'}",
            target=f"web_events.{event_id}.group.{group_id}",
            before="invited", after=row.status,
        ))
        if accept:
            # Re-sync the mirror's desired set. With the flag set this adds
            # the accepting clan's guild; while the event is still a draft
            # (policy 'on_activate') nothing is desired yet — activation
            # picks the flag up then.
            _sync_event_guilds(s, ev)
        s.commit()

        # web96a: record the answer on the challenge thread. The thread stays
        # OPEN afterwards — accepting is the start of the coordination, not
        # the end of it.
        from services.event_invites import announce_response

        announce_response(
            s, event=ev, event_group=row, accepted=accept,
            actor_user_id=user_id,
        )


@event_participants_bp.post("/events/<int:event_id>/participants/<int:group_id>/accept")
async def accept_invitation(event_id: int, group_id: int):
    user_id = current_user_id()
    body = await json_body(required=False)
    mirror = bool(body.get("create_discord_event"))
    await asyncio.to_thread(
        _respond_to_invitation, event_id, group_id, user_id, True, mirror
    )
    return private_no_store(jsonify({"ok": True}))


@event_participants_bp.post("/events/<int:event_id>/participants/<int:group_id>/decline")
async def decline_invitation(event_id: int, group_id: int):
    user_id = current_user_id()
    await asyncio.to_thread(_respond_to_invitation, event_id, group_id, user_id, False)
    return private_no_store(jsonify({"ok": True}))


@event_participants_bp.delete("/events/<int:event_id>/participants/<int:group_id>")
async def remove_participant(event_id: int, group_id: int):
    """Host admin removes a non-host participant (any status). Blocked while
    the clan still has teams on the event — delete or rebind those first, so
    scores and rosters are never silently stranded."""
    user_id = current_user_id()

    def _apply():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _require_clan_vs_clan(ev)
            _assert_event_not_over(ev)
            # Plain host-admin check (no entitlement): a lapsed subscription
            # must not lock the host out of cleaning up its own roster.
            _assert_admin_of_group(s, user_id, ev.group_id)
            row = (
                s.query(EventGroup)
                .filter(EventGroup.event_id == event_id, EventGroup.group_id == group_id)
                .first()
            )
            if not row:
                abort_problem(404, "Not a participant", "That clan is not on this event.")
            if row.role == "host":
                abort_problem(409, "Cannot remove the host",
                              "The host clan cannot be removed from its own event.")
            has_teams = (
                s.query(EventTeam.id)
                .filter(EventTeam.event_id == event_id, EventTeam.group_id == group_id)
                .first()
            )
            if has_teams:
                abort_problem(409, "Clan still has teams",
                              "Remove that clan's teams before removing the clan.")
            was_accepted = row.status == "accepted"
            # web96a: the challenge thread is anchored to this row's id, so
            # close it out BEFORE the delete makes that id unfindable.
            from services.event_invites import announce_withdrawal

            # Commits on its own (nothing of ours is pending yet) so its
            # error path can roll back its own work without discarding the
            # removal we are about to perform.
            announce_withdrawal(
                s, event=ev, event_group=row, actor_user_id=user_id,
            )
            s.delete(row)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                event_id=ev.id,
                action="event.participant.remove",
                target=f"web_events.{event_id}.group.{group_id}",
                before=row.status, after=None,
            ))
            if was_accepted:
                _sync_event_guilds(s, ev)  # their guild leaves the desired set
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
