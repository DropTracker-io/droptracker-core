"""Team leadership for events v2 (web48a).

An event can enable per-team leadership (``web_events.leadership_config``):
each team gets an optional **leader** and, when allowed, a **co-leader** —
roster rows carrying ``web_event_team_members.role``. Leaders hold executive
authority for their team; today that gates board-game turn actions
(roll / shop) via :func:`team_role_for_user`, and co-leaders share it.

Selection is either pure admin assignment or an **election**: every team
member holds one live vote (``web_event_leader_votes``; re-voting replaces
it) and a candidate with a STRICT plurality of the team's cast votes becomes
leader — ties leave the current leader in place, and an admin assignment
always overrides (it does not clear votes; the next vote re-tallies).

Lives in web_api (not services/) so route modules can import it directly:
the unit-test conftest stubs the whole ``services`` package, which makes even
lazy ``services.*`` imports explode inside tested code paths. Pure helpers
stay stdlib-only; db imports are function-local.
"""
from __future__ import annotations

import json
from typing import Optional

LEADER_SELECTION_MODES = ("admin", "election")

DEFAULT_LEADERSHIP = {
    "enabled": False,
    "co_leaders": False,
    "selection": "admin",
}


def effective_leadership(raw_json) -> dict:
    """Full leadership config for one event: defaults overlaid with the stored
    ``web_events.leadership_config`` JSON. Corrupt/unknown data is ignored —
    callers always get every key of :data:`DEFAULT_LEADERSHIP` back."""
    config = dict(DEFAULT_LEADERSHIP)
    if not raw_json:
        return config
    data = raw_json
    if not isinstance(data, dict):
        try:
            data = json.loads(raw_json)
        except (ValueError, TypeError):
            return config
    if not isinstance(data, dict):
        return config
    if "enabled" in data:
        config["enabled"] = bool(data["enabled"])
    if "co_leaders" in data:
        config["co_leaders"] = bool(data["co_leaders"])
    if data.get("selection") in LEADER_SELECTION_MODES:
        config["selection"] = data["selection"]
    return config


def normalize_leadership_input(body) -> Optional[dict]:
    """Validate a PATCH payload's ``leadership`` object into the stored JSON
    shape, or None when invalid. Accepts partial objects (missing keys keep
    their defaults on read)."""
    if not isinstance(body, dict):
        return None
    out = {}
    for key in ("enabled", "co_leaders"):
        if key in body:
            if not isinstance(body[key], bool):
                return None
            out[key] = body[key]
    if "selection" in body:
        if body["selection"] not in LEADER_SELECTION_MODES:
            return None
        out["selection"] = body["selection"]
    return out


def tally_election(votes: list, current_leader: Optional[int]) -> Optional[int]:
    """Pure election tally: ``votes`` are (voter, candidate) player-id pairs
    for ONE team. Returns the player who should lead — a candidate with a
    strict plurality — or ``current_leader`` unchanged on a tie / no votes."""
    counts: dict[int, int] = {}
    for _voter, candidate in votes:
        counts[candidate] = counts.get(candidate, 0) + 1
    if not counts:
        return current_leader
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return current_leader  # tie — nobody takes over
    return ranked[0][0]


def team_leader_ids(session, team_id: int) -> dict[int, str]:
    """{player_id: role} for a team's leader/co-leader roster rows."""
    from db.models import EventTeamMember

    rows = (session.query(EventTeamMember)
            .filter(EventTeamMember.team_id == team_id,
                    EventTeamMember.role.isnot(None))
            .all())
    return {r.player_id: r.role for r in rows}


def team_role_for_user(session, team_id: int, user_id) -> Optional[str]:
    """The leadership role ("leader"/"co_leader") any of ``user_id``'s claimed
    players holds on ``team_id``, or None. The board-game authority check."""
    if user_id is None:
        return None
    from db.models import EventTeamMember, Player

    row = (session.query(EventTeamMember.role)
           .join(Player, Player.player_id == EventTeamMember.player_id)
           .filter(EventTeamMember.team_id == team_id,
                   Player.user_id == user_id,
                   EventTeamMember.role.isnot(None))
           .first())
    return row[0] if row else None


def set_team_role(session, team_id: int, player_id: int, role: Optional[str]) -> bool:
    """Assign ``role`` (or clear with None) on a roster row. A team has at most
    one leader and one co-leader: assigning demotes the current holder of that
    role to plain member first. Returns False when the player isn't on the
    team."""
    from db.models import EventTeamMember

    member = (session.query(EventTeamMember)
              .filter(EventTeamMember.team_id == team_id,
                      EventTeamMember.player_id == player_id)
              .first())
    if member is None:
        return False
    if role:
        (session.query(EventTeamMember)
         .filter(EventTeamMember.team_id == team_id,
                 EventTeamMember.role == role,
                 EventTeamMember.player_id != player_id)
         .update({EventTeamMember.role: None}, synchronize_session=False))
    member.role = role
    return True


def apply_election(session, event_id: int, team_id: int) -> Optional[int]:
    """Re-tally a team's election and promote the winner (if any). Returns the
    resulting leader player_id (or None when no leader)."""
    from db.models import EventLeaderVote, EventTeamMember

    votes = [
        (v.voter_player_id, v.candidate_player_id)
        for v in session.query(EventLeaderVote)
        .filter(EventLeaderVote.event_id == event_id,
                EventLeaderVote.team_id == team_id)
        .all()
    ]
    current = (session.query(EventTeamMember.player_id)
               .filter(EventTeamMember.team_id == team_id,
                       EventTeamMember.role == "leader")
               .first())
    current_leader = current[0] if current else None
    winner = tally_election(votes, current_leader)
    if winner is not None and winner != current_leader:
        if not set_team_role(session, team_id, winner, "leader"):
            return current_leader  # winner left the roster — keep as-is
        return winner
    return current_leader
