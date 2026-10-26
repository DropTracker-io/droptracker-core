"""``/v2/groups/{id}/events`` — a group's events, for display on another site.

Built for partner pages (OSRSClans, ticket 411) that show a clan's upcoming,
running and finished DropTracker events next to its other stats, each linking
back to the event page on droptracker.io.

The listing follows the site's PUBLIC events list, not its admin view, so the
API never shows more than an anonymous visitor would see:

    * ``private`` events are never returned, at any status.
    * There is no "scheduled" status: an event that is booked but not started
      is a draft with a start date, and the lifecycle sweep starts it then.
      Only those drafts count as upcoming. A draft with no start date is
      unpublished, and a draft whose start has passed is stuck on a blocker;
      neither is something a clan has announced, so both are left out.
    * Clan-vs-clan events appear for every group that ACCEPTED them, not only
      the host, matching the group's own list on the site.
"""
from __future__ import annotations

from datetime import datetime, timezone

from quart import Blueprint, g, jsonify, request

from data_api import scope
from data_api.serving import serve_fixed

events_bp = Blueprint("events", __name__)

#: Two small indexed queries for one group. Flat price, so a partner page that
#: refreshes every few minutes for hundreds of clans fits comfortably.
GROUP_EVENTS_COST = 5

#: Past events per response. Upcoming and active are never trimmed (a group
#: has a handful at most); past is the list that grows forever.
DEFAULT_PAST = 10
MAX_PAST = 50

#: Events change status on a minute-scale sweep; a minute of client caching
#: cannot show anything meaningfully stale.
CACHE_SECONDS = 60

_STATUSES = ("upcoming", "active", "past")


def _iso(dt):
    """Stored datetimes are naive UTC; publish them as explicit UTC."""
    if dt is None or not hasattr(dt, "isoformat"):
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _unix(dt):
    if dt is None or not hasattr(dt, "timetuple"):
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def classify(row, now: datetime) -> str | None:
    """Which list ``row`` belongs on, or None when it must not be published.

    Mirrors ``web_api.routes.events._effective_status`` (an active event past
    its end reads as past before the sweep flips it) plus the public-list
    rule for drafts described in the module docstring.
    """
    if (row.visibility or "public") == "private":
        return None
    if row.status == "past":
        return "past"
    if row.status == "draft":
        if row.starts_at is not None and row.starts_at > now:
            return "upcoming"
        return None
    if row.ends_at is not None and row.ends_at < now:
        return "past"
    return "active"


def event_payload(row, now: datetime, site_url: str, counts: dict,
                  group_names: dict) -> dict:
    from services.event_signup import (is_self_signup_mode, signup_close_at,
                                       signups_closed)

    self_signup = is_self_signup_mode(row)
    close_at = signup_close_at(row) if self_signup else None
    host_id = row.group_id
    teams, players = counts.get(row.id, (0, 0))
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or None,
        "status": classify(row, now),
        "kind": row.kind or "standard",
        "mode": row.mode or "standard",
        "starts_at": _iso(row.starts_at),
        "starts_at_unix": _unix(row.starts_at),
        "ends_at": _iso(row.ends_at),
        "ends_at_unix": _unix(row.ends_at),
        # The group running the event. For a clan-vs-clan event this can be
        # another clan (this group is an opponent); staff-hosted events have
        # no host group at all.
        "host_group_id": host_id,
        "host_group_name": group_names.get(host_id) if host_id is not None else None,
        "staff_hosted": (row.mode or "standard") == "clan_vs_clan" and host_id is None,
        "team_count": int(teams),
        "player_count": int(players),
        # Whether players can put themselves on the event right now. False
        # for events where admins place everyone, whatever the dates say.
        "self_signup": self_signup,
        "signups_open": self_signup and signups_closed(row, now) is None,
        "signups_close_at": _iso(close_at),
        "signups_close_at_unix": _unix(close_at),
        "url": f"{site_url}/events/{int(row.id)}",
    }


def build_group_events(session, group_id: int, past_limit: int,
                       wanted: tuple, now: datetime | None = None) -> dict:
    from sqlalchemy import bindparam, text

    from utils.site_urls import WEBSITE_URL

    now = now or datetime.utcnow()
    # Own events plus clan-vs-clan events this group accepted. Private rows
    # and drafts are filtered in SQL as far as SQL can say it, then exactly by
    # classify() so the rule lives in one place.
    rows = session.execute(text("""
        SELECT e.id, e.group_id, e.name, e.description, e.status, e.visibility,
               e.starts_at, e.ends_at, e.kind, e.mode, e.formation_mode,
               e.allow_late_signups, e.activated_at
          FROM web_events e
         WHERE (e.group_id = :gid
                OR e.id IN (SELECT eg.event_id FROM web_event_groups eg
                             WHERE eg.group_id = :gid AND eg.status = 'accepted'))
           AND e.visibility <> 'private'
           AND (e.status <> 'draft' OR e.starts_at > :now)
    """).bindparams(gid=group_id, now=now)).all()

    buckets: dict = {status: [] for status in _STATUSES}
    for row in rows:
        status = classify(row, now)
        if status in wanted:
            buckets[status].append(row)

    far = datetime.max
    buckets["upcoming"].sort(key=lambda r: (r.starts_at or far, r.id))
    buckets["active"].sort(key=lambda r: (r.starts_at or far, r.id))
    # Most recently finished first; an event with no end date sorts by start.
    buckets["past"].sort(key=lambda r: (r.ends_at or r.starts_at or datetime.min, r.id),
                         reverse=True)
    past_total = len(buckets["past"])
    buckets["past"] = buckets["past"][:past_limit]

    shown = [r for status in _STATUSES for r in buckets[status]]
    counts: dict = {}
    group_names: dict = {}
    if shown:
        ids = [r.id for r in shown]
        team_rows = session.execute(text(
            "SELECT event_id, COUNT(*) FROM web_event_teams "
            "WHERE event_id IN :ids GROUP BY event_id"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all()
        player_rows = session.execute(text(
            "SELECT event_id, COUNT(DISTINCT player_id) FROM web_event_team_members "
            "WHERE event_id IN :ids GROUP BY event_id"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all()
        teams = dict(team_rows)
        players = dict(player_rows)
        counts = {i: (teams.get(i, 0), players.get(i, 0)) for i in ids}
        host_ids = sorted({r.group_id for r in shown if r.group_id is not None} | {group_id})
        group_names = dict(session.execute(text(
            "SELECT group_id, group_name FROM groups WHERE group_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": host_ids}).all())

    body = {
        "group_id": group_id,
        "group_name": group_names.get(group_id),
        "events_url": f"{WEBSITE_URL}/events",
        "generated_at": _iso(now),
    }
    for status in _STATUSES:
        if status in wanted:
            body[status] = [event_payload(r, now, WEBSITE_URL, counts, group_names)
                            for r in buckets[status]]
    if "past" in wanted:
        body["past_total"] = past_total
    return body


def _bad_request(detail: str):
    response = jsonify({"error": "bad_request", "detail": detail})
    response.status_code = 400
    return response


@events_bp.route("/groups/<int:group_id>/events", methods=["GET"])
async def group_events(group_id: int):
    """A group's upcoming, active and past events.

    ``?status=`` narrows to a comma list of ``upcoming``, ``active``, ``past``
    (default all three). ``?past_limit=`` caps the past list (default 10,
    max 50); ``past_total`` says how many there are in all.
    """
    key = g.api_key
    if not scope.key_may_read_group(key, group_id):
        if group_id == scope.GLOBAL_GROUP_ID:
            return jsonify({
                "error": "forbidden",
                "detail": "Group 2 is the global pseudo-group and has no events of its own.",
            }), 403
        return jsonify({
            "error": "forbidden",
            "detail": "This key is not scoped to that group.",
        }), 403

    raw_status = (request.args.get("status") or "").strip().lower()
    if raw_status:
        wanted = tuple(s for s in (p.strip() for p in raw_status.split(",")) if s)
        unknown = [s for s in wanted if s not in _STATUSES]
        if unknown or not wanted:
            return _bad_request("status must be a comma list of upcoming, active, past.")
    else:
        wanted = _STATUSES

    raw_limit = request.args.get("past_limit")
    past_limit = DEFAULT_PAST
    if raw_limit is not None:
        try:
            past_limit = int(raw_limit)
        except ValueError:
            return _bad_request("past_limit must be an integer.")
        if not 0 <= past_limit <= MAX_PAST:
            return _bad_request(f"past_limit must be between 0 and {MAX_PAST}.")

    def build(session):
        if not scope.group_exists(session, group_id):
            return None
        return build_group_events(session, group_id, past_limit, wanted)

    return await serve_fixed("groups.events", GROUP_EVENTS_COST, build,
                             not_found_detail="No group with that id.",
                             cache_seconds=CACHE_SECONDS)
