"""Self-service event sign-up + admin pool sorting (shared by the Web API and
the Discord bot).

Two entry points into "a player joins an event":

  • The Web API route ``POST /events/{id}/join`` (web_api/routes/events.py) keeps
    its own inline implementation — it is covered by the standard/global
    regression guardrails and must stay byte-for-byte for existing events.
  • The Discord "Sign up" button (services/message_handler.py) calls
    :func:`perform_signup` here, which applies the *same* rules from the bot
    process.

Plus the admin-only pool operations (:func:`list_pool`, :func:`assign_from_pool`,
:func:`randomize_pool`, :func:`remove_signup`) that back both the Web API's pool
endpoints and, potentially, bot commands.

Module-level imports are stdlib-only (same convention as
``services/event_lifecycle.py``): the unit tests load this file directly, so
the conftest ``db``/``services`` stubs never interfere. DB models are
lazy-imported inside functions.
"""
from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

# Sibling service, stdlib-only at import time for the same reason as this
# module (its db imports are function-local), so importing it here is safe.
from services.event_buyins import sync_buyin_team, sync_buyin_teams


class SignupError(Exception):
    """A user-visible reason a sign-up / pool action was refused. The Web API
    translates ``status``/``title``/``detail`` into an RFC-7807 problem; the
    bot shows ``detail`` in an ephemeral reply."""

    def __init__(self, status: int, title: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail


# ---------------------------------------------------------------------------- #
# Shared rules
# ---------------------------------------------------------------------------- #
def is_self_signup_mode(ev) -> bool:
    from db.models import EVENT_SELF_SIGNUP_MODES

    return (getattr(ev, "formation_mode", None) or "admin_assign") in EVENT_SELF_SIGNUP_MODES


def event_started(ev, now: Optional[datetime] = None) -> bool:
    """Has the event begun? Activation is authoritative (a manual activate can
    beat the schedule); an unactivated draft whose ``starts_at`` has passed
    counts too, since the lifecycle sweep is about to activate it anyway."""
    now = now or datetime.now()
    if getattr(ev, "status", None) in ("active", "past"):
        return True
    starts_at = getattr(ev, "starts_at", None)
    return bool(starts_at and starts_at <= now)


def signup_close_at(ev):
    """When self sign-ups stop being accepted, as a datetime — or None when
    that isn't known yet (an unscheduled draft closes at whatever moment an
    admin activates it). Late sign-ups on: the event's own end."""
    if getattr(ev, "allow_late_signups", False):
        return getattr(ev, "ends_at", None)
    return getattr(ev, "activated_at", None) or getattr(ev, "starts_at", None)


def signups_closed(ev, now: Optional[datetime] = None) -> Optional[str]:
    """The user-visible reason self sign-ups are shut, or None while open.

    Two gates: the event is over (always closing), or the event has begun and
    ``allow_late_signups`` is off (web70a — the default). Admin placement is a
    separate surface and stays open until the event is past.
    """
    now = now or datetime.now()
    ends_at = getattr(ev, "ends_at", None)
    if getattr(ev, "status", None) == "past" or (ends_at and ends_at < now):
        return "This event is over — sign-ups are closed."
    if not getattr(ev, "allow_late_signups", False) and event_started(ev, now):
        return "Sign-ups closed when the event began."
    return None


def assert_signups_open(ev, now: Optional[datetime] = None) -> None:
    """:func:`signups_closed` as a guard. Raises :class:`SignupError` 409."""
    reason = signups_closed(ev, now)
    if reason:
        raise SignupError(409, "Sign-ups closed", reason)


def _is_clan_vs_clan(ev) -> bool:
    return (getattr(ev, "mode", None) or "standard") == "clan_vs_clan"


def player_group_ids(session, player_id: int) -> set:
    """Group ids the player belongs to (via user_group_association)."""
    from db.models import user_group_association

    return {
        gid for (gid,) in
        session.query(user_group_association.c.group_id)
        .filter(user_group_association.c.player_id == player_id)
        .all()
    }


def participating_group_ids(session, ev) -> set:
    """Mirror of web_api.routes.events.participating_group_ids so the bot need
    not import the web layer: accepted participants for clan_vs_clan, else the
    single owning group (empty for global)."""
    if _is_clan_vs_clan(ev):
        from db.models import EventGroup

        return {
            gid for (gid,) in
            session.query(EventGroup.group_id)
            .filter(EventGroup.event_id == ev.id, EventGroup.status == "accepted")
            .all()
        }
    return {ev.group_id} if ev.group_id else set()


def assert_player_eligible(session, ev, player_id: int) -> None:
    """The player must belong to a participating group (any, for clan_vs_clan;
    the single group for a standard group event). Global events: anyone."""
    gids = participating_group_ids(session, ev)
    if not gids:
        return
    if not (player_group_ids(session, player_id) & gids):
        raise SignupError(403, "Not a group member",
                          "That account is not a member of a participating clan.")


# The message a player sees when they can't be auto-signed-up because they
# belong to several clans competing in the SAME event (G7). The only resolution
# is a manual admin add to a specific team, so we tell them exactly that.
MULTI_CLAN_SIGNUP_MESSAGE = (
    "You're a member of more than one clan taking part in this event, so we "
    "can't add you to a team automatically — we don't know which side you're "
    "on. Ask a leader of the clan you want to play for to add you to one of "
    "their teams."
)


def participating_clans_for_player(session, ev, player_id: int) -> set:
    """The participating clans (clan_vs_clan) this player belongs to. Empty for
    standard/global events. More than one is the ambiguous case handled by
    :func:`assert_single_participating_clan`."""
    if not _is_clan_vs_clan(ev):
        return set()
    return player_group_ids(session, player_id) & participating_group_ids(session, ev)


def assert_single_participating_clan(session, ev, player_id: int) -> None:
    """Block self/auto sign-up for a player in MULTIPLE participating clans
    (clan_vs_clan, G7): the system can't infer which side they're on, so an
    admin must place them explicitly. No-op for standard/global events and for a
    player in exactly one participating clan (zero is caught by eligibility)."""
    if len(participating_clans_for_player(session, ev, player_id)) > 1:
        raise SignupError(409, "Multiple clans", MULTI_CLAN_SIGNUP_MESSAGE)


def signup_group_for_player(session, ev, player_id: int) -> Optional[int]:
    """Which group a player signs up *under*: their participating clan for
    clan_vs_clan, else the event's group (None for global). Assumes the caller
    has already ruled out multi-clan ambiguity (see
    :func:`assert_single_participating_clan`)."""
    if _is_clan_vs_clan(ev):
        return next(iter(participating_clans_for_player(session, ev, player_id)), None)
    return ev.group_id


def eligible_teams(session, ev, player_id: int) -> list:
    """Teams a player may be placed on, id-ascending. clan_vs_clan restricts to
    the player's own clan's teams; otherwise all of the event's teams."""
    from db.models import EventTeam

    teams = (
        session.query(EventTeam)
        .filter(EventTeam.event_id == ev.id)
        .order_by(EventTeam.id.asc())
        .all()
    )
    if _is_clan_vs_clan(ev):
        my = player_group_ids(session, player_id)
        teams = [t for t in teams if t.group_id and t.group_id in my]
    return teams


def user_player_ids(session, user_id: int) -> set:
    from db.models import Player

    return {pid for (pid,) in session.query(Player.player_id).filter(Player.user_id == user_id).all()}


def existing_entry_player_id(session, ev, user_id: int) -> Optional[int]:
    """If any of the user's accounts already has a sign-up OR a team membership
    on this event, return that player_id (enforces one RSN per user per event
    on the self-service path). None if the user has no entry yet."""
    from db.models import EventSignup, EventTeam, EventTeamMember

    pids = user_player_ids(session, user_id)
    if not pids:
        return None
    row = (
        session.query(EventSignup.player_id)
        .filter(EventSignup.event_id == ev.id, EventSignup.player_id.in_(pids))
        .first()
    )
    if row:
        return row[0]
    row = (
        session.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == ev.id, EventTeamMember.player_id.in_(pids))
        .first()
    )
    return row[0] if row else None


# ---------------------------------------------------------------------------- #
# The self-service join (Discord button; the Web API route mirrors this inline)
# ---------------------------------------------------------------------------- #
def perform_signup(session, ev, player, user_id: int, team_id: Optional[int] = None,
                   source: str = "web", join_code: Optional[str] = None) -> dict:
    """Sign ``player`` (a Player owned by ``user_id``) up for ``ev``.

    Enforces: sign-ups still open (:func:`signups_closed` — over, or begun
    without ``allow_late_signups``), self-signup mode, eligibility, join code
    (self_join), one RSN per user. Then, by formation mode:
      • signup_pool → records a sign-up with no team (admins sort later);
      • self_join   → requires/uses ``team_id`` (auto when the player's clan has
        exactly one team);
      • auto_assign → balances onto the player's smallest eligible team;
      • solo        → a new team of their own (free-for-all).
    Returns ``{"team_id": int|None, "pooled": bool}``. Raises :class:`SignupError`.
    The caller owns the commit.
    """
    formation = getattr(ev, "formation_mode", None) or "admin_assign"
    if not is_self_signup_mode(ev):
        raise SignupError(403, "Sign-ups closed",
                          "This event's teams are set by its admins.")
    # Over, or begun without late sign-ups enabled (web70a). Checked after the
    # mode so an admin-assigned event keeps its precise "admins place players"
    # answer rather than a window message it never had.
    assert_signups_open(ev)
    if formation == "self_join" and ev.join_code:
        if not isinstance(join_code, str) or join_code.strip() != ev.join_code:
            raise SignupError(403, "Join code required", "The join code is missing or wrong.")

    assert_player_eligible(session, ev, player.player_id)
    # G7: a player in several participating clans can't be auto-placed — which
    # side would they be on? Block every self sign-up mode (pool included) and
    # route them to a manual admin add to a specific team.
    assert_single_participating_clan(session, ev, player.player_id)

    # One RSN per user per event: if a DIFFERENT account of the user already
    # entered, refuse. The same account re-signing up falls through to an
    # idempotent upsert / "already placed" check below.
    other = existing_entry_player_id(session, ev, user_id)
    if other is not None and other != player.player_id:
        raise SignupError(409, "Already signed up",
                          "You've already entered this event with another account. "
                          "Only one account per person can take part.")

    from db.models import EventSignup, EventTeamMember

    group_id = signup_group_for_player(session, ev, player.player_id)

    # Record / upsert the opt-in row (single source of truth for the pool view).
    signup = (
        session.query(EventSignup)
        .filter(EventSignup.event_id == ev.id, EventSignup.player_id == player.player_id)
        .first()
    )
    if signup is None:
        signup = EventSignup(event_id=ev.id, player_id=player.player_id,
                             group_id=group_id, user_id=user_id, source=source)
        session.add(signup)

    if formation == "signup_pool":
        # No team yet — admins sort the pool later.
        return {"team_id": None, "pooled": True}

    if formation == "solo":
        # Free-for-all: the entrant's own team, made on the spot (idempotent).
        team_id = ensure_solo_team(session, ev, player.player_id)
        sync_buyin_team(session, ev.id, player.player_id, team_id)
        return {"team_id": team_id, "pooled": False}

    teams = eligible_teams(session, ev, player.player_id)
    if not teams:
        if _is_clan_vs_clan(ev):
            raise SignupError(404, "No team", "Your clan has no team on this event yet.")
        raise SignupError(404, "No teams", "This event has no teams to join yet.")

    # Already placed? Idempotent.
    placed = (
        session.query(EventTeamMember)
        .filter(EventTeamMember.player_id == player.player_id,
                EventTeamMember.team_id.in_([t.id for t in teams]))
        .first()
    )
    if placed:
        return {"team_id": placed.team_id, "pooled": False}

    if formation == "self_join":
        if team_id is None and len(teams) == 1:
            team = teams[0]
        else:
            team = next((t for t in teams if t.id == team_id), None)
            if team is None:
                raise SignupError(422, "Pick a team", "Choose one of your clan's teams to join.")
    else:  # auto_assign — balance onto the smallest eligible team.
        counts = _team_counts(session, [t.id for t in teams])
        team = min(teams, key=lambda t: (counts.get(t.id, 0), t.id))

    session.add(EventTeamMember(team_id=team.id, player_id=player.player_id,
                                event_id=team.event_id))
    return {"team_id": team.id, "pooled": False}


def _team_counts(session, team_ids: list) -> dict:
    from db.models import EventTeamMember
    from sqlalchemy import func as _func

    if not team_ids:
        return {}
    return dict(
        session.query(EventTeamMember.team_id, _func.count(EventTeamMember.player_id))
        .filter(EventTeamMember.team_id.in_(team_ids))
        .group_by(EventTeamMember.team_id)
        .all()
    )


# ---------------------------------------------------------------------------- #
# Free-for-all (formation_mode == "solo"): every entrant is a team of one
# ---------------------------------------------------------------------------- #
def is_solo(ev) -> bool:
    return (getattr(ev, "formation_mode", None) or "admin_assign") == "solo"


def solo_blocked_reason(kind: Optional[str], mode: Optional[str]) -> Optional[str]:
    """Why an event of this ``kind``/ownership ``mode`` can't run
    free-for-all, or None when it can. Pure."""
    from db.models import EVENT_SOLO_BLOCKED_KINDS, EVENT_SOLO_BLOCKED_MODES

    if (mode or "standard") in EVENT_SOLO_BLOCKED_MODES:
        return ("Clan-vs-clan teams stand for clans, so the event can't be "
                "free-for-all.")
    if (kind or "standard") in EVENT_SOLO_BLOCKED_KINDS:
        return {
            "conquest": "Conquest needs rival teams fighting over the map.",
        }.get(kind, "Skill/Boss of the Week already ranks every player on "
                    "their own.")
    return None


def solo_team_name(player_name: Optional[str], player_id: int, taken: set) -> str:
    """The team name for one free-for-all entrant: their RSN, suffixed
    " (2)", " (3)"… if another team already uses it (case-insensitively).
    Pure; adds the chosen name to ``taken`` (lower-cased)."""
    base = (str(player_name).strip() if player_name else "") or f"Player {player_id}"
    base = base[:72]  # EventTeam.name is 80 wide; leave room for a suffix
    name, n = base, 1
    while name.lower() in taken:
        n += 1
        name = f"{base} ({n})"
    taken.add(name.lower())
    return name


def _taken_team_names(session, event_id: int) -> set:
    from db.models import EventTeam

    return {
        (name or "").lower() for (name,) in
        session.query(EventTeam.name).filter(EventTeam.event_id == event_id).all()
    }


def create_solo_teams(session, ev, player_ids, joined_at: Optional[dict] = None) -> list:
    """Give each player in ``player_ids`` a fresh team of their own on ``ev``
    and place them on it. ``joined_at`` optionally maps player_id → the
    membership's credit cutoff to keep (a split must not move it). Returns
    ``[(player_id, team_id)]``. Caller has already checked nobody is placed;
    flushes, caller owns the commit."""
    from db.models import EventTeam, EventTeamMember, Player

    player_ids = list(dict.fromkeys(player_ids))
    if not player_ids:
        return []
    names = dict(
        session.query(Player.player_id, Player.player_name)
        .filter(Player.player_id.in_(player_ids)).all()
    )
    taken = _taken_team_names(session, ev.id)
    teams = []
    for pid in player_ids:
        team = EventTeam(event_id=ev.id,
                         name=solo_team_name(names.get(pid), pid, taken),
                         score=0)
        session.add(team)
        teams.append((pid, team))
    session.flush()  # assign team ids
    placements = []
    for pid, team in teams:
        member = EventTeamMember(team_id=team.id, player_id=pid, event_id=ev.id)
        cutoff = (joined_at or {}).get(pid)
        if cutoff is not None:
            member.joined_at = cutoff
        session.add(member)
        placements.append((pid, team.id))
    session.flush()
    return placements


def ensure_solo_team(session, ev, player_id: int) -> int:
    """The free-for-all sign-up: the player's existing team on ``ev`` if they
    are already placed (idempotent), else a new team of their own. Returns the
    team id. Caller owns the commit (and the IntegrityError backstop for a
    concurrent double-join — uq_web_evt_member_event_player)."""
    from db.models import EventTeamMember

    placed = (
        session.query(EventTeamMember.team_id)
        .filter(EventTeamMember.event_id == ev.id,
                EventTeamMember.player_id == player_id)
        .first()
    )
    if placed:
        return placed[0]
    [(_, team_id)] = create_solo_teams(session, ev, [player_id])
    return team_id


def drop_empty_solo_team(session, ev, team_id: int) -> bool:
    """After a free-for-all entrant leaves: delete their now-empty team unless
    it already scored (credited ledger rows / a non-zero score), in which case
    it stays on the standings like any departed member's history. Returns
    whether the team was deleted. Caller owns the commit."""
    from db.models import EventCompletion, EventTeam, EventTeamMember

    team = (session.query(EventTeam)
            .filter(EventTeam.id == team_id, EventTeam.event_id == ev.id)
            .first())
    if team is None:
        return False
    if session.query(EventTeamMember.player_id).filter(
            EventTeamMember.team_id == team_id).first():
        return False
    if float(team.score or 0) or session.query(EventCompletion.id).filter(
            EventCompletion.team_id == team_id).first():
        return False
    from services.event_team_purge import purge_team

    purge_team(session, ev.id, team)
    return True


def plan_solo_split(teams: list) -> dict:
    """Pure: which teams a switch to free-for-all breaks up. ``teams`` is
    ``[(team_id, [player_id, ...])]``. A team with one member already IS that
    player's team and stays; an empty team is left for the admins. Returns
    ``{"split": {team_id: [player_ids]}, "kept": [team_id], "empty": [team_id]}``."""
    split, kept, empty = {}, [], []
    for team_id, members in teams:
        if len(members) > 1:
            split[team_id] = sorted(members)
        elif members:
            kept.append(team_id)
        else:
            empty.append(team_id)
    return {"split": split, "kept": kept, "empty": empty}


def split_into_solo_teams(session, ev) -> dict:
    """Turn every multi-member team on ``ev`` into one team per member — the
    conversion behind switching a live event to free-for-all.

    Each member keeps their credit cutoff (``joined_at``) and takes their own
    ledger rows, effort rows and buy-ins to their new team (bingo-cell
    credits are re-derived instead); every task's rollups are then re-folded
    from the ledger (``recompute_task_rollups``, silently — no lead-change
    announcement), so
    each player's progress, laps, score and contribution points are what they
    would have been had they entered alone. The emptied teams are deleted.
    Ledger rows with no player (line/blackout bonuses, team-only manual
    awards) can't be attributed to anyone and go with their team —
    ``unattributed_rows`` counts them.

    Refused for board games once begun (positions, coins and rolls are team
    state that can't be divided). Returns a summary; flushes, caller owns the
    commit (and the matcher bump)."""
    from db.models import (EventCompletion, EventEffort, EventLeaderVote,
                           EventTask, EventTeam, EventTeamMember)
    from services.event_team_purge import purge_team

    rows = (
        session.query(EventTeam.id, EventTeamMember.player_id,
                      EventTeamMember.joined_at)
        .outerjoin(EventTeamMember, EventTeamMember.team_id == EventTeam.id)
        .filter(EventTeam.event_id == ev.id)
        .order_by(EventTeam.id.asc())
        .all()
    )
    members_by_team: dict = {}
    joined: dict = {}
    for team_id, pid, joined_at in rows:
        members_by_team.setdefault(team_id, [])
        if pid is not None:
            members_by_team[team_id].append(pid)
            joined[pid] = joined_at
    plan = plan_solo_split(list(members_by_team.items()))
    summary = {"split_teams": len(plan["split"]), "new_teams": 0,
               "moved_rows": 0, "unattributed_rows": 0, "kept_teams": len(plan["kept"]),
               "empty_teams": len(plan["empty"]), "recomputed_tasks": 0}
    if not plan["split"]:
        return summary
    if (getattr(ev, "kind", None) or "standard") == "board_game" and \
            getattr(ev, "status", None) in ("active", "past"):
        raise SignupError(409, "Board already in play",
                          "A board game's teams can't be split up once it has "
                          "begun — positions and coins belong to the team.")

    old_ids = list(plan["split"])
    moving = [pid for pids in plan["split"].values() for pid in pids]
    # Off the old rosters first (uq_web_evt_member_event_player), then onto
    # teams of their own.
    session.query(EventTeamMember).filter(
        EventTeamMember.team_id.in_(old_ids)
    ).delete(synchronize_session=False)
    session.query(EventLeaderVote).filter(
        EventLeaderVote.team_id.in_(old_ids)
    ).delete(synchronize_session=False)
    session.flush()
    placements = dict(create_solo_teams(
        session, ev, moving, joined_at={pid: joined.get(pid) for pid in moving}))
    summary["new_teams"] = len(placements)

    for pid, new_tid in placements.items():
        # Bingo-cell credits are NOT moved: the re-fold below re-derives them
        # per new team from its own completed flag (a moved row would outlive
        # a task the player alone never finished).
        for model in (EventCompletion, EventEffort):
            moved = (session.query(model)
                     .filter(model.team_id.in_(old_ids), model.player_id == pid)
                     .update({model.team_id: new_tid}, synchronize_session=False))
            if model is EventCompletion:
                summary["moved_rows"] += moved
    sync_buyin_teams(session, ev.id, placements)
    summary["unattributed_rows"] = (
        session.query(EventCompletion.id)
        .filter(EventCompletion.team_id.in_(old_ids)).count())
    session.flush()

    # Re-fold every task: the old teams drop to nothing, each new team earns
    # its own laps. Board games/conquest never reach here with rows to move.
    from services.event_engine import recompute_task_rollups

    for task in (session.query(EventTask)
                 .filter(EventTask.event_id == ev.id)
                 .order_by(EventTask.id.asc()).all()):
        try:
            recompute_task_rollups(session, ev, task, announce_lead=False,
                                   regroup=True)
            summary["recomputed_tasks"] += 1
        except ValueError:
            continue  # forward_only kinds (board game / conquest / race task)

    for team in session.query(EventTeam).filter(EventTeam.id.in_(old_ids)).all():
        purge_team(session, ev.id, team)
    session.flush()
    return summary


# ---------------------------------------------------------------------------- #
# Admin pool operations (signup_pool)
# ---------------------------------------------------------------------------- #
def list_pool(session, ev) -> list:
    """The sign-up pool with each player's current team placement (None while
    unassigned) plus the roster-building context an admin sorts on:
    ``ehb`` (WOM efficient hours bossed) and ``total_level`` off the Player.
    Ordered by sign-up time.

    Monthly loot is *not* joined here — it lives in Redis, not the DB — so the
    Web API layer enriches each row with ``monthly_loot`` on top of this result
    (see ``web_api.common.player_month_totals``). Keeping this function DB-only
    lets the bot share it without pulling in the web layer."""
    from db.models import EventSignup, EventTeam, EventTeamMember, Group, Player

    rows = (
        session.query(EventSignup, Player.player_name, Group.group_name,
                      Player.ehb, Player.total_level)
        .join(Player, Player.player_id == EventSignup.player_id)
        .outerjoin(Group, Group.group_id == EventSignup.group_id)
        .filter(EventSignup.event_id == ev.id)
        .order_by(EventSignup.created_at.asc())
        .all()
    )
    if not rows:
        return []
    pids = [s.player_id for s, *_ in rows]
    # Current placement for each signed-up player on THIS event.
    placement = dict(
        session.query(EventTeamMember.player_id, EventTeamMember.team_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == ev.id, EventTeamMember.player_id.in_(pids))
        .all()
    )
    return [
        {
            "player_id": s.player_id,
            "player_name": pname,
            "group_id": s.group_id,
            "group_name": gname,
            "team_id": placement.get(s.player_id),
            "source": s.source,
            "signed_up_at": int(s.created_at.timestamp()) if s.created_at else None,
            "ehb": round(float(ehb), 1) if ehb is not None else None,
            "total_level": int(total_level) if total_level is not None else None,
        }
        for s, pname, gname, ehb, total_level in rows
    ]


def _signed_up(session, ev, player_id: int) -> bool:
    from db.models import EventSignup

    return (
        session.query(EventSignup.id)
        .filter(EventSignup.event_id == ev.id, EventSignup.player_id == player_id)
        .first()
        is not None
    )


def _place(session, event_id: int, player_id: int, team_id: int) -> None:
    """(Re)place a player onto a team: delete any existing membership on the
    event, then insert a fresh row (joined_at resets — the credit cutoff).

    Does NOT move the player's buy-ins — the callers do that, in one batched
    UPDATE per operation (see ``services/event_buyins.py``), because
    :func:`randomize_pool` places the whole pool in a loop."""
    from db.models import EventTeam, EventTeamMember

    existing = (
        session.query(EventTeamMember)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == event_id, EventTeamMember.player_id == player_id)
        .all()
    )
    for m in existing:
        session.delete(m)
    session.flush()
    session.add(EventTeamMember(team_id=team_id, player_id=player_id,
                                event_id=event_id))


def assign_from_pool(session, ev, player_id: int, team_id: int) -> None:
    """Place one signed-up player onto a specific team (admin sort). Validates
    the player is in the pool and the team is eligible (right clan). The caller
    owns the commit."""
    from db.models import EventTeam

    if not _signed_up(session, ev, player_id):
        raise SignupError(404, "Not in the pool", "That player has not signed up for this event.")
    team = (
        session.query(EventTeam)
        .filter(EventTeam.id == team_id, EventTeam.event_id == ev.id)
        .first()
    )
    if not team:
        raise SignupError(404, "Team not found", f"No team {team_id} in this event.")
    if _is_clan_vs_clan(ev):
        if not team.group_id or team.group_id not in player_group_ids(session, player_id):
            raise SignupError(422, "Wrong clan",
                              "That team belongs to a different clan than the player.")
    _place(session, ev.id, player_id, team_id)
    # web71a: the draft has happened — a buy-in this player paid at sign-up
    # (recorded with no team) now credits the team they were sorted onto.
    sync_buyin_team(session, ev.id, player_id, team_id)


def unassign_from_pool(session, ev, player_id: int) -> None:
    """Move a signed-up player back to the pool: drop their team placement on
    this event but KEEP the sign-up (unlike :func:`remove_signup`, which
    withdraws them entirely). Lets an admin undo a mis-assignment without
    forcing the player to sign up again. No-op if they hold no placement. The
    caller owns the commit."""
    from db.models import EventTeam, EventTeamMember

    if not _signed_up(session, ev, player_id):
        raise SignupError(404, "Not in the pool", "That player has not signed up for this event.")
    memberships = (
        session.query(EventTeamMember)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == ev.id, EventTeamMember.player_id == player_id)
        .all()
    )
    for m in memberships:
        session.delete(m)
    # web71a: back in the pool -> their buy-in goes back to the unassigned
    # bucket. The GP stays in the pot; it just stops crediting a team.
    sync_buyin_team(session, ev.id, player_id, None)


def randomize_pool(session, ev, group_id: Optional[int] = None) -> dict:
    """Distribute the sign-up pool across teams at random, balanced. Repeatable
    — every call reshuffles every signed-up player (existing placements are
    replaced). clan_vs_clan keeps each clan's players on that clan's teams.
    Optionally scope to a single ``group_id`` (re-roll just one clan). Returns
    ``{"assigned": n, "unassigned": m}``. The caller owns the commit."""
    from db.models import EventSignup, EventTeam

    signups = session.query(EventSignup).filter(EventSignup.event_id == ev.id)
    if group_id is not None:
        signups = signups.filter(EventSignup.group_id == group_id)
    signups = signups.all()

    teams = session.query(EventTeam).filter(EventTeam.event_id == ev.id).all()
    teams_by_group: dict = {}
    for t in teams:
        # For clan_vs_clan the eligible bucket is the team's clan; otherwise a
        # single shared bucket (None) holds every team.
        key = t.group_id if _is_clan_vs_clan(ev) else None
        teams_by_group.setdefault(key, []).append(t)

    assigned = 0
    unassigned = 0
    placements: dict = {}   # player_id -> team_id, for the buy-in carry-over
    # Group sign-ups by the bucket they draw teams from.
    by_bucket: dict = {}
    for s in signups:
        key = s.group_id if _is_clan_vs_clan(ev) else None
        by_bucket.setdefault(key, []).append(s)

    for key, bucket_signups in by_bucket.items():
        bucket_teams = teams_by_group.get(key) or []
        if not bucket_teams:
            unassigned += len(bucket_signups)
            continue
        order = list(bucket_signups)
        random.shuffle(order)
        # Round-robin from a random starting team keeps sizes within 1 of each
        # other while still shuffling who lands where.
        start = random.randrange(len(bucket_teams))
        for i, s in enumerate(order):
            team = bucket_teams[(start + i) % len(bucket_teams)]
            _place(session, ev.id, s.player_id, team.id)
            placements[s.player_id] = team.id
            assigned += 1
    # web71a: one UPDATE per destination team carries every sign-up-time buy-in
    # onto the team the shuffle just dealt its payer — a re-roll re-points them
    # again, so the pot's per-team split always matches the current draft.
    sync_buyin_teams(session, ev.id, placements)
    return {"assigned": assigned, "unassigned": unassigned}


# ---------------------------------------------------------------------------- #
# Admin scale/testing tool: random bulk population
# ---------------------------------------------------------------------------- #
# "Active" reuses the ONLY player-recency threshold that already exists in the
# codebase: data/player_total_updater.py marks a Player stale for a WOM refresh
# once ``Player.date_updated < now - timedelta(days=14)``. The inverse — updated
# within the last 14 days — is our "active member" definition here. Do not fork
# this number; if the staleness window there changes, change it here too.
ACTIVE_MEMBER_WINDOW_DAYS = 14

# Hard ceiling on how many members one populate-random call adds (P1-12) — a
# scale-testing tool should fill hundreds/low-thousands per call, not the whole
# player base in a single transaction.
POPULATE_MAX_ADDED = 2000


def _plan_distribution(selected_by_bucket: dict, teams_by_bucket: dict,
                       start_counts: dict) -> list:
    """Pure placement planner (no DB): assign each selected player to the
    least-full eligible team in its bucket (ties -> lowest team id), accounting
    for teams' existing sizes so the result stays balanced. Returns a list of
    ``(player_id, team_id)``. Players in a bucket with no team are dropped.

    ``selected_by_bucket``: {bucket_key: [player_id, ...]} (already shuffled/capped)
    ``teams_by_bucket``:    {bucket_key: [team_id, ...]}
    ``start_counts``:       {team_id: current_member_count}
    """
    counts = dict(start_counts)
    placements: list = []
    for bucket, pids in selected_by_bucket.items():
        tids = teams_by_bucket.get(bucket) or []
        if not tids:
            continue
        for pid in pids:
            tid = min(tids, key=lambda t: (counts.get(t, 0), t))
            placements.append((pid, tid))
            counts[tid] = counts.get(tid, 0) + 1
    return placements


def populate_random(session, ev, *, source: str, count: Optional[int] = None) -> dict:
    """Admin scale/testing tool: bulk-fill this event's teams with randomly
    chosen ACTIVE members, balanced across teams (clan-aware). Only ADDS players
    not already on the event — never moves or removes anyone.

    ``source``:
      • ``"group"``  — draw from the event's linked group(s) (the accepted
        participants for clan_vs_clan, the single owning group otherwise).
      • ``"global"`` — draw from every active player. This only differs from
        ``"group"`` for *global* events (no participating groups); a group or
        clan event can only ever place its own members (team eligibility), so
        both sources fall back to the participating-member pool there.

    ``count`` optionally caps how many are added (after shuffling, so it's a
    random sample). Returns ``{added, source, teams: [{team_id, team_name,
    added, member_count}]}``. Raises :class:`SignupError`. Caller owns commit.
    """
    from datetime import timedelta

    from db.models import EventTeam, EventTeamMember, Player, user_group_association

    if source not in ("group", "global"):
        raise SignupError(422, "Invalid source", "'source' must be 'group' or 'global'.")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count <= 0):
        raise SignupError(422, "Invalid count", "'count' must be a positive integer.")

    teams = (
        session.query(EventTeam)
        .filter(EventTeam.event_id == ev.id)
        .order_by(EventTeam.id.asc())
        .all()
    )
    if not teams:
        raise SignupError(409, "No teams", "Add at least one team before auto-populating.")

    participating = participating_group_ids(session, ev)
    if source == "group" and not participating:
        raise SignupError(
            409, "No linked group",
            "This event has no group to draw members from. Use the global source instead.",
        )

    cutoff = datetime.now() - timedelta(days=ACTIVE_MEMBER_WINDOW_DAYS)
    clan_vs_clan = _is_clan_vs_clan(ev)

    # Players already on the event are skipped — this tool only adds.
    placed = {
        pid for (pid,) in
        session.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == ev.id)
        .all()
    }

    # Candidate pool, bucketed by the clan whose teams they can join (a single
    # shared None bucket for non-clan events). A group/clan event can only place
    # its own members, so both sources draw from participating members there;
    # only a global event (no participating groups) draws the whole player base.
    cand_buckets: dict = {}
    if participating:
        rows = (
            session.query(user_group_association.c.player_id,
                          user_group_association.c.group_id)
            .join(Player, Player.player_id == user_group_association.c.player_id)
            .filter(user_group_association.c.group_id.in_(participating),
                    user_group_association.c.player_id.isnot(None),
                    Player.date_updated >= cutoff)
            .all()
        )
        seen: set = set()
        for pid, gid in rows:
            if pid is None or pid in placed or pid in seen:
                continue
            seen.add(pid)
            key = gid if clan_vs_clan else None
            cand_buckets.setdefault(key, []).append(pid)
    else:
        pids = [
            pid for (pid,) in
            session.query(Player.player_id)
            .filter(Player.date_updated >= cutoff)
            .all()
            if pid not in placed
        ]
        cand_buckets[None] = pids

    teams_by_bucket: dict = {}
    for t in teams:
        key = t.group_id if clan_vs_clan else None
        teams_by_bucket.setdefault(key, []).append(t.id)

    # Drop candidates whose bucket has no team, shuffle globally, then cap.
    # P1-12: always bound the placement count. Each placement is ~4 statements
    # (_place = select/delete/flush/insert) inside one transaction; an uncapped
    # global populate ("fill everyone") would fire tens of thousands and hold
    # membership locks past the query timeout. The explicit `count` still wins
    # when smaller.
    eligible = [
        (key, pid)
        for key, pids in cand_buckets.items() if teams_by_bucket.get(key)
        for pid in pids
    ]
    random.shuffle(eligible)
    cap = min(count, POPULATE_MAX_ADDED) if count is not None else POPULATE_MAX_ADDED
    eligible = eligible[:cap]

    selected_by_bucket: dict = {}
    for key, pid in eligible:
        selected_by_bucket.setdefault(key, []).append(pid)

    start_counts = _team_counts(session, [t.id for t in teams])
    placements = _plan_distribution(selected_by_bucket, teams_by_bucket, start_counts)

    # Reuse the shared placement primitive (delete-any-existing + insert) that
    # backs the admin roster-add path — no raw inserts, so joined_at (the credit
    # cutoff) and all membership invariants hold.
    for pid, tid in placements:
        _place(session, ev.id, pid, tid)

    per_team_added: dict = {}
    for _, tid in placements:
        per_team_added[tid] = per_team_added.get(tid, 0) + 1
    teams_summary = [
        {
            "team_id": t.id,
            "team_name": t.name,
            "added": per_team_added.get(t.id, 0),
            "member_count": start_counts.get(t.id, 0) + per_team_added.get(t.id, 0),
        }
        for t in teams
    ]
    return {"added": len(placements), "source": source, "teams": teams_summary}


# ---------------------------------------------------------------------------- #
# Manager action: sign up every member of the participating clan(s)
# ---------------------------------------------------------------------------- #
# Ceiling on one call (the populate tool's P1-12 reasoning: bound the rows one
# transaction writes). Sign-ups are single inserts, so this sits above
# POPULATE_MAX_ADDED; a clan larger than this re-runs the action for the rest.
SIGNUP_ALL_MAX_ADDED = 5000


def _pick_one_per_user(rows: list) -> tuple:
    """Pure (no DB): one account per person. ``rows`` are ``(player_id,
    user_id, date_updated)``; unclaimed accounts (user_id None) each stand
    alone. A user with several accounts in the clan keeps the most recently
    updated one (ties: lowest player_id). Returns ``(kept_ids, alt_count)``."""
    best: dict = {}
    kept: list = []
    for pid, uid, updated in rows:
        if uid is None:
            kept.append(pid)
            continue
        cur = best.get(uid)
        key = (updated or datetime.min, -pid)
        if cur is None or key > cur[0]:
            best[uid] = (key, pid)
    kept.extend(pid for _, pid in best.values())
    return sorted(kept), len(rows) - len(kept)


def signup_all_members(session, ev, group_id: Optional[int] = None) -> dict:
    """Manager action: sign up every current member of the event's clan(s) —
    the one-shot counterpart of a SOTW/BOTW "whole clan" roster, for any event
    kind. Only ADDS: anyone already signed up or on a team is left alone.

    Members come from ``user_group_association`` (the WOM-synced roster) of
    the participating groups, optionally scoped to one ``group_id`` (a
    clan_vs_clan clan). Same rules as a self sign-up, applied in bulk:
      • one account per person — a user who already entered with any account
        is skipped, and of several accounts in the clan only the most
        recently updated is entered (``skipped_alts``);
      • clan_vs_clan players in more than one participating clan are skipped
        (G7 — an admin must place them; ``skipped_multi_clan``);
      • clans whose team already follows the clan automatically (``auto_clan``)
        are skipped — their roster is the clan.
    Then by formation mode: ``signup_pool`` leaves everyone in the pool for
    the admins to sort; ``solo`` gives each of them a team of their own; every
    other mode also places them, balanced onto the least-full eligible team
    (what an auto-assign sign-up does). Returns
    counts; ``capped`` is true when :data:`SIGNUP_ALL_MAX_ADDED` cut it short.
    Raises :class:`SignupError`. The caller owns the commit (and, on a
    staff-hosted event, the roster-capacity check against ``per_team``)."""
    from db.models import (EventSignup, EventTeam, EventTeamMember, Player,
                           user_group_association)

    groups = participating_group_ids(session, ev)
    if group_id is not None:
        if group_id not in groups:
            raise SignupError(422, "Not a participating clan",
                              "That clan isn't taking part in this event.")
        groups = {group_id}
    if not groups:
        raise SignupError(409, "No linked group",
                          "This event has no clan whose members could be signed up.")

    formation = getattr(ev, "formation_mode", None) or "admin_assign"
    clan_vs_clan = _is_clan_vs_clan(ev)
    teams = (
        session.query(EventTeam)
        .filter(EventTeam.event_id == ev.id)
        .order_by(EventTeam.id.asc())
        .all()
    )
    # A team that follows its clan automatically (SOTW/BOTW whole clan, CvC
    # whole-clan teams) already holds every member; never double-enter them.
    auto_groups = {
        (t.group_id if clan_vs_clan else ev.group_id)
        for t in teams if getattr(t, "auto_clan", False)
    }
    groups -= auto_groups
    if not groups:
        raise SignupError(409, "Already the whole clan",
                          "This event already enters every clan member automatically.")
    place = formation != "signup_pool"
    solo = formation == "solo"
    if place and not teams and not solo:
        raise SignupError(409, "No teams", "Add at least one team first.")

    # Every membership row across ALL participating clans (not just the
    # scoped ones): G7 ambiguity is "in more than one competing clan".
    all_groups = participating_group_ids(session, ev)
    rows = (
        session.query(user_group_association.c.player_id,
                      user_group_association.c.group_id,
                      Player.user_id, Player.date_updated)
        .join(Player, Player.player_id == user_group_association.c.player_id)
        .filter(user_group_association.c.group_id.in_(all_groups if clan_vs_clan else groups),
                user_group_association.c.player_id.isnot(None))
        .all()
    )
    member_groups: dict = {}
    info: dict = {}
    for pid, gid, uid, updated in rows:
        member_groups.setdefault(pid, set()).add(gid)
        info[pid] = (uid, updated)
    members = [pid for pid, gids in member_groups.items() if gids & groups]
    multi = {pid for pid in members if clan_vs_clan and len(member_groups[pid]) > 1}

    signed = {
        pid for (pid,) in
        session.query(EventSignup.player_id).filter(EventSignup.event_id == ev.id).all()
    }
    placed = {
        pid for (pid,) in
        session.query(EventTeamMember.player_id)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == ev.id)
        .all()
    }
    entered = signed | placed
    # One RSN per user: a user who already entered with ANY account is done.
    entered_users = {
        uid for (uid,) in
        session.query(Player.user_id)
        .filter(Player.player_id.in_(list(entered)), Player.user_id.isnot(None))
        .all()
    } if entered else set()

    already = sum(1 for pid in members if pid in entered)
    fresh = [pid for pid in members if pid not in entered and pid not in multi]
    other_account = [pid for pid in fresh
                     if info[pid][0] is not None and info[pid][0] in entered_users]
    kept, alts = _pick_one_per_user([
        (pid, info[pid][0], info[pid][1]) for pid in fresh
        if not (info[pid][0] is not None and info[pid][0] in entered_users)
    ])
    capped = len(kept) > SIGNUP_ALL_MAX_ADDED
    kept = kept[:SIGNUP_ALL_MAX_ADDED]

    def _clan(pid):
        if not clan_vs_clan:
            return ev.group_id
        return next(iter(member_groups[pid] & groups))

    session.add_all([
        EventSignup(event_id=ev.id, player_id=pid, group_id=_clan(pid),
                    user_id=info[pid][0], source="bulk")
        for pid in kept
    ])

    placements: list = []
    if solo:
        # Free-for-all: everyone gets a team of their own.
        session.flush()
        placements = create_solo_teams(session, ev, kept)
        sync_buyin_teams(session, ev.id, dict(placements))
    elif place:
        placements = _plan_bulk_placement(ev, teams, kept, _clan, session)
        session.add_all([
            EventTeamMember(team_id=tid, player_id=pid, event_id=ev.id)
            for pid, tid in placements
        ])
        # A buy-in recorded before placement now credits the dealt team.
        sync_buyin_teams(session, ev.id, dict(placements))

    per_team: dict = {}
    for _, tid in placements:
        per_team[tid] = per_team.get(tid, 0) + 1
    return {
        "added": len(kept),
        "placed": len(placements),
        "unplaced": len(kept) - len(placements) if place else 0,
        "pooled": not place,
        "already": already,
        "skipped_alts": alts + len(other_account),
        "skipped_multi_clan": len(multi - entered),
        "capped": capped,
        # {team_id: players added} — the route's roster-capacity check.
        "per_team": per_team,
    }


def _plan_bulk_placement(ev, teams, player_ids, clan_of, session) -> list:
    """Balanced placement for :func:`signup_all_members`: clan_vs_clan players
    go to their clan's teams, everyone else shares the event's teams. Teams
    that follow their clan automatically are never targets."""
    clan_vs_clan = _is_clan_vs_clan(ev)
    targets = [t for t in teams if not getattr(t, "auto_clan", False)]
    teams_by_bucket: dict = {}
    for t in targets:
        teams_by_bucket.setdefault(t.group_id if clan_vs_clan else None, []).append(t.id)
    selected: dict = {}
    for pid in player_ids:
        selected.setdefault(clan_of(pid) if clan_vs_clan else None, []).append(pid)
    start_counts = _team_counts(session, [t.id for t in targets])
    return _plan_distribution(selected, teams_by_bucket, start_counts)


def remove_signup(session, ev, player_id: int) -> None:
    """Withdraw a sign-up and any resulting team placement. Caller owns commit."""
    from db.models import EventSignup, EventTeam, EventTeamMember

    session.query(EventSignup).filter(
        EventSignup.event_id == ev.id, EventSignup.player_id == player_id
    ).delete(synchronize_session=False)
    memberships = (
        session.query(EventTeamMember)
        .join(EventTeam, EventTeam.id == EventTeamMember.team_id)
        .filter(EventTeam.event_id == ev.id, EventTeamMember.player_id == player_id)
        .all()
    )
    left = [m.team_id for m in memberships]
    for m in memberships:
        session.delete(m)
    # web71a: a withdrawn player's buy-in (if any) stops crediting a team. The
    # row itself stands — voiding paid GP is a separate, deliberate pot action.
    sync_buyin_team(session, ev.id, player_id, None)
    if is_solo(ev):
        session.flush()
        for team_id in left:
            drop_empty_solo_team(session, ev, team_id)
