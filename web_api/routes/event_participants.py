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
from web_api.common import abort_problem, db_session, private_no_store
from web_api.deps import (
    current_user_id,
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
    """Owner/admin of ``group_id`` (or superadmin) — no entitlement check:
    responding to an invite must never require the invited clan to pay."""
    user = load_user(s, user_id)
    if is_superadmin(user):
        return
    role = resolve_group_role(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
    if role not in ("owner", "admin"):
        abort_problem(403, "Forbidden", "You must be an admin of that clan.")


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
    return {
        gid for gid in candidates
        if resolve_group_role(s, user_id, gid, mgids, user=user) in ("owner", "admin")
    }


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
            s.add(EventGroup(
                event_id=event_id, group_id=group_id, role="opponent",
                status="invited", invited_by_user_id=user_id,
            ))
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                action="event.participant.invite",
                target=f"web_events.{event_id}.group.{group_id}",
                before=None, after="invited",
            ))
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))


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
            return [
                {
                    "group_id": r.group_id,
                    "group_name": name,
                    "role": r.role,
                    "status": r.status,
                    "invited_at": _ts(r.created_at),
                    "responded_at": _ts(r.responded_at),
                }
                for r, name in rows
            ]

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
            out = []
            for r, ev, gname in rows:
                if _effective_status(ev) == "past":
                    continue  # expired invite — nothing actionable
                out.append({
                    "event": _summary(ev),
                    "group_id": r.group_id,
                    "group_name": gname,
                    "host_group_name": host_names.get(ev.group_id),
                    "invited_at": _ts(r.created_at),
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
            actor_user_id=user_id, group_id=group_id,
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
            s.delete(row)
            s.add(AuditLog(
                actor_user_id=user_id, group_id=ev.group_id,
                action="event.participant.remove",
                target=f"web_events.{event_id}.group.{group_id}",
                before=row.status, after=None,
            ))
            if was_accepted:
                _sync_event_guilds(s, ev)  # their guild leaves the desired set
            s.commit()

    await asyncio.to_thread(_apply)
    return private_no_store(jsonify({"ok": True}))
