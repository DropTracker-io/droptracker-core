"""Event clan-point awards (web114a) — web surface.

  GET  /api/v1/events/{id}/clan-points?group_id=&preview=1
  PUT  /api/v1/events/{id}/clan-points          { group_id?, config: {...} }
  POST /api/v1/events/{id}/clan-points/award    { group_id? }
  POST /api/v1/events/{id}/clan-points/revoke   { group_id?, confirm: "REVOKE" }

One payout per clan (``web_event_point_configs``): a standard event's own
group, or each accepted clan of a clan-vs-clan event — ``group_id`` picks it
and defaults to the event's group. Reads follow the event's own visibility;
the public sees each clan's offer ("1st: +100 each …") and, once paid, who got
what. Per-player participation points (and the EHE hours behind them) are
omitted when the event keeps EHE to its admins (``effort_visibility``): the
amount is the hours with a multiplier on it.

Writes need the clan's admins or event managers (the owner's call: event
managers run the clan's events end to end), and turning a payout on needs the
clan's points system — the ``custom_points`` entitlement; superadmins may
configure regardless, but nothing is ever *paid* into a clan whose points
system is off. Awarding needs the event to be over. Scoring, the diff/re-sync
ledger and the auto-at-end path live in services/event_point_awards.py.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from quart import Blueprint, jsonify, request

from db import AuditLog, EventTeam, Group, Player
from db.models import EventPointAward, EventPointConfig
from web_api.common import (
    abort_problem,
    db_session,
    hidden_player_ids,
    private_no_store,
    with_cache_headers,
)
from web_api.deps import (
    current_user_id,
    is_event_manager,
    is_group_admin_role,
    is_superadmin,
    json_body,
    load_user,
    manageable_guild_ids,
    optional_user_id,
    resolve_group_role,
)
from web_api.routes.events import (
    _can_view_restricted,
    _deny_restricted,
    _effort_visible,
    _is_event_admin,
    _is_restricted,
    _load_event_or_404,
    _ts,
)

event_points_bp = Blueprint("v1_event_points", __name__)


def _svc():
    # Lazy: the unit-test conftest stubs the ``services`` package.
    from services import event_point_awards

    return event_point_awards


def _scope_ids(s, ev) -> list:
    return _svc().scope_group_ids(s, ev)


def _resolve_group(s, ev, raw) -> int:
    """The clan a write targets: ``group_id`` from the body/query, defaulting
    to the event's own group. Must be a clan that takes part."""
    scopes = _scope_ids(s, ev)
    if not scopes:
        abort_problem(422, "No clan to pay",
                      "Clan points are paid into a clan's points — a global event "
                      "has no clan to award them in.",
                      extra={"code": "clan_points_no_clan"})
    if raw in (None, ""):
        group_id = ev.group_id if ev.group_id in scopes else scopes[0]
    else:
        try:
            group_id = int(raw)
        except (TypeError, ValueError):
            abort_problem(422, "Invalid group", "group_id must be an integer.")
    if group_id not in scopes:
        abort_problem(404, "Clan not in this event",
                      f"Group {group_id} doesn't take part in this event.")
    return group_id


def _can_manage(s, user_id, user, group_id: int) -> bool:
    if user_id is None:
        return False
    if is_superadmin(user):
        return True
    role = resolve_group_role(s, user_id, group_id, manageable_guild_ids(user_id), user=user)
    return is_group_admin_role(role) or is_event_manager(s, user_id, group_id)


def _assert_manager(s, user_id, group_id: int):
    user = load_user(s, user_id)
    if not _can_manage(s, user_id, user, group_id):
        abort_problem(403, "Forbidden",
                      "Only this clan's admins and event managers can manage its "
                      "clan-point awards.",
                      extra={"code": "clan_points_manager_required"})
    return user


def _assert_viewable(s, viewer_id, ev) -> None:
    if _is_restricted(ev) and not _can_view_restricted(s, viewer_id, ev):
        _deny_restricted(ev, viewer_id)
        abort_problem(404, "Event not found", f"No event {ev.id}.")


def _awarded_block(s, ev, group_id: int, *, team_names: dict, admin: bool,
                   reveal_participation: bool, hidden: set) -> dict:
    """What the event paid this clan, one row per player (placement and
    participation folded together), best-paid first."""
    rows = (s.query(EventPointAward, Player.player_name)
            .join(Player, Player.player_id == EventPointAward.player_id)
            .filter(EventPointAward.event_id == ev.id,
                    EventPointAward.group_id == group_id)
            .all())
    per: dict = {}
    for award, name in rows:
        entry = per.setdefault(award.player_id, {
            "player_id": award.player_id,
            "player_name": name or f"Player {award.player_id}",
            "team_id": award.team_id,
            "team_name": team_names.get(award.team_id),
            "place": None, "placement": 0, "participation": 0, "hours": None,
        })
        if award.kind == "placement":
            entry["placement"] += int(award.amount or 0)
            entry["place"] = award.place
        else:
            entry["participation"] += int(award.amount or 0)
            entry["hours"] = award.hours
            if entry["team_id"] is None:
                entry["team_id"] = award.team_id
                entry["team_name"] = team_names.get(award.team_id)
    entries = list(per.values())
    totals = {
        "players": len(entries),
        "placement_points": sum(e["placement"] for e in entries),
        "participation_points": sum(e["participation"] for e in entries),
        "participation_players": sum(1 for e in entries if e["participation"]),
    }
    totals["total_points"] = totals["placement_points"] + totals["participation_points"]
    out_rows = []
    for e in entries:
        e["total"] = e["placement"] + e["participation"]
        if not admin:
            if not reveal_participation:
                # EHE is admins-only on this event: the per-player figure
                # would be the hours with a multiplier on it.
                if not e["placement"]:
                    continue
                e["participation"] = None
                e["hours"] = None
                e["total"] = e["placement"]
            if e["player_id"] in hidden:
                e["player_id"] = None
                e["player_name"] = "Hidden player"
        out_rows.append(e)
    out_rows.sort(key=lambda r: (-(r["total"] or 0), r["place"] or 10 ** 6,
                                 str(r["player_name"] or "").lower()))
    return {**totals, "rows": out_rows}


def _preview_block(s, ev, group_id: int, row, *, team_names: dict) -> dict:
    """The admin preview: what the event would pay this clan right now, why
    it can't be awarded yet (if it can't), and how far an existing award has
    drifted from it."""
    svc = _svc()
    config = svc.effective_points_config(row.config if row is not None else None)
    plan = svc.compute_plan(s, ev, group_id, config)
    stub = row if row is not None else SimpleNamespace(config=None, group_id=group_id,
                                                       status="pending")
    blocker = svc.award_blocker(s, ev, stub, plan)
    changes = None
    if row is not None and row.status == "awarded":
        changes = svc.pending_changes(s, ev, group_id, config, plan)

    def _with_team(r):
        return {**r, "team_name": team_names.get(r.get("team_id"))}

    return {
        **svc.summarize(plan),
        "rows": [_with_team(r) for r in plan["rows"]],
        "skipped": [_with_team(r) for r in plan["skipped"]],
        "placements": plan["placements"],
        "competition": plan["competition"],
        "ehe_supported": plan["ehe_supported"],
        "rates_known": plan["rates_known"],
        "roster_size": plan["roster_size"],
        "blocker": blocker,
        "changes": changes,
        "out_of_sync": bool(changes and any(changes[k] for k in ("insert", "update", "remove"))),
    }


def _scope_payload(s, ev, group_id: int, row, *, group_name, manage: bool,
                   preview: bool, team_names: dict, reveal_participation: bool,
                   admin_view: bool, hidden: set) -> dict:
    svc = _svc()
    config = svc.effective_points_config(row.config if row is not None else None)
    status = row.status if row is not None else "pending"
    out = {
        "group_id": group_id,
        "group_name": group_name,
        # The clan's points system is live (custom_points) — without it
        # nothing can be paid, so the UI shows the upgrade path instead.
        "available": svc.points_system_active(group_id),
        "can_manage": manage,
        "config": config,
        "status": status,
        "awarded_at": _ts(row.awarded_at) if row is not None else None,
        "last_error": row.last_error if (row is not None and manage) else None,
        "awarded": (_awarded_block(s, ev, group_id, team_names=team_names,
                                   admin=admin_view or manage,
                                   reveal_participation=reveal_participation,
                                   hidden=hidden)
                    if status == "awarded" else None),
    }
    if manage and preview:
        out["preview"] = _preview_block(s, ev, group_id, row, team_names=team_names)
    return out


def _payload(s, ev, viewer_id, *, only_group=None, preview: bool = False) -> dict:
    svc = _svc()
    scope_ids = _scope_ids(s, ev)
    if only_group is not None:
        scope_ids = [g for g in scope_ids if g == only_group]
    rows = {
        r.group_id: r for r in s.query(EventPointConfig)
        .filter(EventPointConfig.event_id == ev.id).all()
    }
    names = dict(s.query(Group.group_id, Group.group_name)
                 .filter(Group.group_id.in_(scope_ids)).all()) if scope_ids else {}
    team_names = dict(s.query(EventTeam.id, EventTeam.name)
                      .filter(EventTeam.event_id == ev.id).all())
    user = load_user(s, viewer_id) if viewer_id is not None else None
    admin_view = _is_event_admin(s, viewer_id, ev)
    reveal = _effort_visible(s, viewer_id, ev)
    hidden = hidden_player_ids()
    scopes = []
    for gid in scope_ids:
        row = rows.get(gid)
        manage = _can_manage(s, viewer_id, user, gid)
        config = svc.effective_points_config(row.config if row is not None else None)
        public = row is not None and (
            row.status == "awarded" or svc.pays_anything(config))
        if not (manage or public):
            continue
        scopes.append(_scope_payload(
            s, ev, gid, row, group_name=names.get(gid), manage=manage,
            preview=preview, team_names=team_names, reveal_participation=reveal,
            admin_view=admin_view, hidden=hidden))
    return {
        "event_id": ev.id,
        "status": ev.status,
        "kind": getattr(ev, "kind", None) or "standard",
        "scopes": scopes,
    }


@event_points_bp.get("/events/<int:event_id>/clan-points")
async def get_clan_points(event_id: int):
    viewer_id = optional_user_id()
    raw_group = request.args.get("group_id")
    preview = (request.args.get("preview") or "").strip() in ("1", "true")

    def _load():
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            _assert_viewable(s, viewer_id, ev)
            only = None
            if raw_group not in (None, ""):
                only = _resolve_group(s, ev, raw_group)
            return _payload(s, ev, viewer_id, only_group=only, preview=preview)

    payload = await asyncio.to_thread(_load)
    if viewer_id is not None:
        return private_no_store(jsonify(payload))
    return with_cache_headers(jsonify(payload), max_age=15)


@event_points_bp.put("/events/<int:event_id>/clan-points")
async def put_clan_points(event_id: int):
    user_id = current_user_id()
    body = await json_body()

    def _apply():
        svc = _svc()
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            group_id = _resolve_group(s, ev, body.get("group_id"))
            user = _assert_manager(s, user_id, group_id)
            try:
                patch = svc.normalize_points_input(body.get("config") or {})
            except svc.PointsConfigError as exc:
                abort_problem(422, "Invalid clan points config", str(exc),
                              extra={"code": "invalid_clan_points"})
            row = svc.load_config_row(s, ev.id, group_id, for_update=True)
            before = svc.effective_points_config(row.config if row is not None else None)
            merged = svc.merge_points_config(row.config if row is not None else None, patch)
            if (merged["enabled"] and not before["enabled"]
                    and not svc.points_system_active(group_id) and not is_superadmin(user)):
                abort_problem(
                    403, "Subscription required",
                    "Clan points need this clan's points system — the Clan Points "
                    "feature on its subscription.",
                    extra={"code": "entitlement_required", "entitlement": "custom_points"})
            if row is None:
                row = EventPointConfig(event_id=ev.id, group_id=group_id, status="pending")
                s.add(row)
            row.config = json.dumps(merged)
            row.updated_by_user_id = user_id
            if row.status == "deferred" and (merged["award_mode"] != "auto"
                                             or not merged["enabled"]):
                # Only auto payouts are retried; anything else waits for an
                # admin, who sees the Award button.
                row.status = "pending"
            if merged != before:
                s.add(AuditLog(
                    actor_user_id=user_id, group_id=group_id, event_id=ev.id,
                    action="event.points.config",
                    target=f"web_event_point_configs.{ev.id}:{group_id}",
                    before=json.dumps(before), after=json.dumps(merged),
                ))
            s.commit()
            return _payload(s, ev, user_id, only_group=group_id, preview=True)

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@event_points_bp.post("/events/<int:event_id>/clan-points/award")
async def award_clan_points(event_id: int):
    """Award — or re-sync an existing award to the current standings. Only
    the difference is written, so calling it twice is harmless."""
    user_id = current_user_id()
    body = await json_body(required=False) or {}

    def _apply():
        svc = _svc()
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            group_id = _resolve_group(s, ev, body.get("group_id"))
            _assert_manager(s, user_id, group_id)
            row = svc.load_config_row(s, ev.id, group_id, for_update=True)
            if row is None:
                abort_problem(409, "Nothing to award",
                              "Set up this clan's clan-point awards first.",
                              extra={"code": "clan_points_unconfigured"})
            config = svc.effective_points_config(row.config)
            plan = svc.compute_plan(s, ev, group_id, config)
            blocker = svc.award_blocker(s, ev, row, plan)
            if blocker:
                abort_problem(409, "Can't award clan points yet", blocker,
                              extra={"code": "clan_points_blocked"})
            summary = svc.apply_award(s, ev, row, actor_user_id=user_id, plan=plan)
            s.commit()
            svc.publish_update(ev.id, group_id, summary)
            return {
                "summary": {k: v for k, v in summary.items() if k != "placements"},
                **_payload(s, ev, user_id, only_group=group_id, preview=True),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))


@event_points_bp.post("/events/<int:event_id>/clan-points/revoke")
async def revoke_clan_points(event_id: int):
    """Take back every point the event paid this clan. Needs the literal
    ``{"confirm": "REVOKE"}`` — it edits members' balances."""
    user_id = current_user_id()
    body = await json_body()
    if str(body.get("confirm") or "").strip().upper() != "REVOKE":
        abort_problem(400, "Confirmation required",
                      'Send { "confirm": "REVOKE" } to remove this event\'s clan points.')

    def _apply():
        svc = _svc()
        with db_session() as s:
            ev = _load_event_or_404(s, event_id)
            group_id = _resolve_group(s, ev, body.get("group_id"))
            _assert_manager(s, user_id, group_id)
            row = svc.load_config_row(s, ev.id, group_id, for_update=True)
            if row is None or row.status != "awarded":
                abort_problem(409, "Nothing to revoke",
                              "This event hasn't paid this clan any clan points.",
                              extra={"code": "clan_points_not_awarded"})
            summary = svc.revoke_awards(s, ev, row, actor_user_id=user_id)
            s.commit()
            svc.publish_update(ev.id, group_id, summary)
            return {
                "summary": summary,
                **_payload(s, ev, user_id, only_group=group_id, preview=True),
            }

    return private_no_store(jsonify(await asyncio.to_thread(_apply)))
