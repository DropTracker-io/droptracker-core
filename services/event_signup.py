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
      • auto_assign → balances onto the player's smallest eligible team.
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
    event, then insert a fresh row (joined_at resets — the credit cutoff)."""
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
            assigned += 1
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
    for m in memberships:
        session.delete(m)
