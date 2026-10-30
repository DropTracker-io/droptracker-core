"""``GET /v2/events/{id}/standings`` — who placed where, and who was on each team.

Built for a clan's own bot to pay its own rewards at the end of an event (the
Discord delivery relay's "Award points" button, web153a), so the placements
are the ones DropTracker's own clan-point awards use
(``services/event_point_awards``): ties share a place and the next is skipped
("1, 1, 3"), a team or player that scored nothing is not placed, board games
rank by the finish-line race, and an individual SOTW/BOTW ranks players under
its own ranking mode (from the stored WOM standings — no live WOM call here).

Visibility follows the site:

    * A public event that has started can be read by any key, as anyone can
      see its standings page.
    * A private event, or a draft, is readable only by a key of the hosting
      group or of a clan that ACCEPTED it (clan-vs-clan). Everyone else gets
      the same 404 as a missing event, so ids can't be probed.
    * Hidden players are left out of the member lists (privacy parity); the
      team's score and place still count them, as on the site.

Each member carries ``took_part`` (any credited completion or tracked effort —
the bar DropTracker's own placement payouts use) and, for a group key,
``in_group`` (a current member of the key's group), so a bot can pay exactly
the people DropTracker would.
"""
from __future__ import annotations

from datetime import timezone

from quart import Blueprint, g

from data_api import scope
from data_api.serving import serve_fixed

event_standings_bp = Blueprint("event_standings", __name__)

STANDINGS_COST = 10
CACHE_SECONDS = 30
EVENT_URL = "https://www.droptracker.io/events/{id}"


def _iso(dt):
    if dt is None or not hasattr(dt, "isoformat"):
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _num(value):
    """Scores are stored as floats; publish whole numbers as ints."""
    v = float(value or 0)
    return int(v) if v.is_integer() else round(v, 2)


def participating_group_ids(session, event) -> set:
    from db.models import EventGroup

    ids = {event.group_id} if event.group_id else set()
    ids |= {gid for (gid,) in session.query(EventGroup.group_id).filter(
        EventGroup.event_id == event.id, EventGroup.status == "accepted").all()}
    return ids


def may_read_event(session, key: dict, event) -> bool:
    if (getattr(event, "visibility", None) or "public") == "public" and event.status != "draft":
        return True
    if key.get("owner_type") == "group" and key.get("group_id"):
        return int(key["group_id"]) in participating_group_ids(session, event)
    return False


def build_standings(session, event, viewer_group_id=None) -> dict:
    """The response body for one event (read-only)."""
    from db.models import EventTeam, EventTeamMember, Player
    from services import event_point_awards as epa

    competition_kind = epa._is_competition(event)
    individual = competition_kind and not epa._is_team_race(session, event)
    body = {
        "event": {
            "id": event.id,
            "name": event.name,
            "kind": getattr(event, "kind", None) or "standard",
            "mode": getattr(event, "mode", None) or "standard",
            "status": event.status,
            "final": event.status == "past",
            "group_id": event.group_id,
            "starts_at": _iso(event.starts_at),
            "ends_at": _iso(event.ends_at),
            "ended_at": _iso(getattr(event, "ended_at", None)),
            "url": EVENT_URL.format(id=event.id),
        },
        "ranks": "players" if individual else "teams",
    }

    if individual:
        ranked, mode = epa._competition_standings(session, event)
        places = epa._competition_placements(ranked, mode)
        pids = [int(r["player_id"]) for r in ranked if r.get("player_id") is not None]
        visible = set(scope.visible_player_ids(session, pids))
        in_group = (epa._clan_member_ids(session, viewer_group_id, pids)
                    if viewer_group_id else set())
        players = []
        for r in ranked:
            pid = r.get("player_id")
            if pid is not None and int(pid) not in visible:
                continue
            value = r.get("points") if mode == "points" else r.get("gained")
            entry = {
                "place": places.get(int(pid)) if pid is not None else None,
                "player_id": int(pid) if pid is not None else None,
                "player_name": r.get("player_name") or r.get("name"),
                "value": int(value or 0),
                "registered": bool(r.get("registered", pid is not None)),
            }
            if viewer_group_id:
                entry["in_group"] = pid is not None and int(pid) in in_group
            players.append(entry)
        body["ranking_mode"] = mode
        body["players"] = players
        return body

    placements, _labels = epa._team_placements(session, event)
    teams = (session.query(EventTeam).filter(EventTeam.event_id == event.id)
             .order_by(EventTeam.id).all())
    rows = (session.query(EventTeamMember.team_id, EventTeamMember.player_id,
                          Player.player_name)
            .join(Player, Player.player_id == EventTeamMember.player_id)
            .filter(EventTeamMember.team_id.in_([t.id for t in teams] or [0]))
            .all())
    pids = sorted({int(pid) for _tid, pid, _n in rows})
    visible = set(scope.visible_player_ids(session, pids))
    took = epa._took_part(session, event.id, pids)
    in_group = epa._clan_member_ids(session, viewer_group_id, pids) if viewer_group_id else set()
    members: dict = {}
    for tid, pid, name in rows:
        if int(pid) not in visible:
            continue
        entry = {"player_id": int(pid), "player_name": name or f"Player {pid}",
                 "took_part": int(pid) in took}
        if viewer_group_id:
            entry["in_group"] = int(pid) in in_group
        members.setdefault(tid, []).append(entry)
    out = []
    for t in teams:
        out.append({
            "place": placements.get(t.id),
            "team_id": t.id,
            "name": t.name,
            "score": _num(t.score),
            "group_id": getattr(t, "group_id", None),
            "members": sorted(members.get(t.id, []), key=lambda m: m["player_name"].lower()),
        })
    out.sort(key=lambda t: (t["place"] is None, t["place"] or 0, -float(t["score"]), t["team_id"]))
    body["teams"] = out
    return body


@event_standings_bp.route("/events/<int:event_id>/standings", methods=["GET"])
async def event_standings(event_id: int):
    """Current standings for a running event; final ones once it has ended
    (``event.final``). See the module docstring for the shape."""
    key = g.api_key
    viewer = int(key["group_id"]) if key.get("owner_type") == "group" and key.get("group_id") else None

    def build(session):
        from db.models import Event

        event = session.query(Event).filter(Event.id == event_id).first()
        if event is None or not may_read_event(session, key, event):
            return None
        return build_standings(session, event, viewer)

    return await serve_fixed("events.standings", STANDINGS_COST, build,
                             not_found_detail="No event with that id.",
                             cache_seconds=CACHE_SECONDS)
