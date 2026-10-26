"""``/v2/groups/{id}/events`` lists what the public site lists, and no more.

Partner sites (OSRSClans, ticket 411) render this unauthenticated to their own
visitors, so the rules that keep events off the site's public list must hold
here too: private events never appear, and of the drafts only the ones with a
future start date (booked, auto-starting) count as upcoming.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 10, 26, 12, 0, 0)


def _row(id, status="active", visibility="public", starts=None, ends=None,
         group_id=7, kind="bingo", mode="standard", formation="self_join",
         late=False, activated=None, description=None):
    return types.SimpleNamespace(
        id=id, group_id=group_id, name=f"Event {id}", description=description,
        status=status, visibility=visibility, starts_at=starts, ends_at=ends,
        kind=kind, mode=mode, formation_mode=formation,
        allow_late_signups=late, activated_at=activated)


@pytest.fixture(autouse=True)
def _signup_rules(monkeypatch):
    """The real sign-up rules, minus their db/services imports (stubbed here)."""
    mod = types.ModuleType("services.event_signup")
    self_modes = ("self_join", "auto_assign", "signup_pool", "solo")

    def is_self_signup_mode(ev):
        return (ev.formation_mode or "admin_assign") in self_modes

    def signup_close_at(ev):
        if ev.allow_late_signups:
            return ev.ends_at
        return ev.activated_at or ev.starts_at

    def signups_closed(ev, now=None):
        if ev.status == "past" or (ev.ends_at and ev.ends_at < now):
            return "over"
        started = ev.status in ("active", "past") or bool(ev.starts_at and ev.starts_at <= now)
        if not ev.allow_late_signups and started:
            return "started"
        return None

    mod.is_self_signup_mode = is_self_signup_mode
    mod.signup_close_at = signup_close_at
    mod.signups_closed = signups_closed
    monkeypatch.setitem(sys.modules, "services.event_signup", mod)
    site = types.ModuleType("utils.site_urls")
    site.WEBSITE_URL = "https://www.droptracker.io"
    monkeypatch.setitem(sys.modules, "utils.site_urls", site)


class _FakeSession:
    """Answers the builder's four queries: events, team counts, player counts, names."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM web_events" in sql:
            return types.SimpleNamespace(all=lambda: list(self.rows))
        if "FROM web_event_teams" in sql:
            return types.SimpleNamespace(all=lambda: [(i, 2) for i in params["ids"]])
        if "FROM web_event_team_members" in sql:
            return types.SimpleNamespace(all=lambda: [(i, 9) for i in params["ids"]])
        if "FROM groups" in sql:
            return types.SimpleNamespace(
                all=lambda: [(gid, f"Group {gid}") for gid in params["ids"]])
        raise AssertionError(sql)


def test_classify_follows_public_list_rules():
    from data_api.routes.events import classify

    day = timedelta(days=1)
    assert classify(_row(1, "draft", starts=NOW + day), NOW) == "upcoming"
    assert classify(_row(2, "draft", starts=None), NOW) is None          # unpublished
    assert classify(_row(3, "draft", starts=NOW - day), NOW) is None     # stuck draft
    assert classify(_row(4, "active", starts=NOW - day, ends=NOW + day), NOW) == "active"
    assert classify(_row(5, "active", ends=NOW - day), NOW) == "past"    # pre-sweep
    assert classify(_row(6, "past"), NOW) == "past"
    for status in ("draft", "active", "past"):
        assert classify(_row(7, status, "private", starts=NOW + day), NOW) is None


def test_builder_buckets_sorts_and_trims_past():
    from data_api.routes.events import build_group_events

    day = timedelta(days=1)
    rows = [
        _row(10, "draft", starts=NOW + 3 * day, description="Spooky"),
        _row(11, "draft", starts=NOW + 1 * day),
        _row(12, "active", starts=NOW - day, ends=NOW + day),
        _row(13, "past", starts=NOW - 30 * day, ends=NOW - 20 * day),
        _row(14, "past", starts=NOW - 10 * day, ends=NOW - 5 * day),
        _row(15, "past", starts=NOW - 50 * day, ends=NOW - 40 * day),
        _row(16, "active", "private", starts=NOW - day),
    ]
    body = build_group_events(_FakeSession(rows), 7, 2,
                              ("upcoming", "active", "past"), now=NOW)

    assert [e["id"] for e in body["upcoming"]] == [11, 10]   # soonest first
    assert [e["id"] for e in body["active"]] == [12]
    assert [e["id"] for e in body["past"]] == [14, 13]       # latest end first, trimmed
    assert body["past_total"] == 3
    assert body["group_name"] == "Group 7"

    upcoming = body["upcoming"][1]
    assert upcoming["description"] == "Spooky"
    assert upcoming["url"] == "https://www.droptracker.io/events/10"
    assert upcoming["starts_at"].endswith("Z")
    assert upcoming["self_signup"] is True and upcoming["signups_open"] is True
    assert upcoming["team_count"] == 2 and upcoming["player_count"] == 9
    # Started without late sign-ups: closed.
    assert body["active"][0]["signups_open"] is False


def test_admin_assigned_event_never_reports_open_signups():
    from data_api.routes.events import build_group_events

    rows = [_row(20, "draft", starts=NOW + timedelta(days=2), formation="admin_assign")]
    body = build_group_events(_FakeSession(rows), 7, 10, ("upcoming",), now=NOW)
    event = body["upcoming"][0]
    assert event["self_signup"] is False
    assert event["signups_open"] is False
    assert event["signups_close_at"] is None
    assert "active" not in body and "past" not in body and "past_total" not in body


def test_clan_vs_clan_reports_the_host():
    from data_api.routes.events import build_group_events

    rows = [_row(30, "active", starts=NOW - timedelta(days=1), group_id=99,
                 mode="clan_vs_clan"),
            _row(31, "active", starts=NOW - timedelta(days=1), group_id=None,
                 mode="clan_vs_clan")]
    body = build_group_events(_FakeSession(rows), 7, 10, ("active",), now=NOW)
    hosted, staff = body["active"]
    assert hosted["host_group_id"] == 99 and hosted["host_group_name"] == "Group 99"
    assert hosted["staff_hosted"] is False
    assert staff["host_group_id"] is None and staff["staff_hosted"] is True


# --------------------------------------------------------------------------- #
# Route plumbing: scope, params, unknown group.
# --------------------------------------------------------------------------- #
class _Decision:
    allowed = True
    reason = ""
    retry_after = None
    headers = {"X-RateLimit-Cost": "5"}


class _Slot:
    acquired = True

    def __init__(self, *_args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _client(monkeypatch, key_scope="global", key_group=None):
    import data_api
    import data_api.auth as auth
    import data_api.routes.events as events_route
    import data_api.scope as scope
    import data_api.serving as serving
    import data_api.usage as usage
    from quart import g

    async def _authenticate():
        g.api_key = {
            "key_id": 1, "label": "test", "tier": "partner",
            "scope": key_scope, "owner_type": key_scope,
            "owner_user_id": None, "group_id": key_group,
            "limits": {"requests_per_min": 100, "cost_units_per_min": 10 ** 9,
                       "requests_per_day": 10 ** 6, "max_concurrency": 4},
        }
        return None

    monkeypatch.setattr(auth, "authenticate_request", _authenticate)
    monkeypatch.setattr(serving, "SessionLocal", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(serving, "check_and_charge", lambda *a, **k: _Decision())
    monkeypatch.setattr(serving, "Concurrency", _Slot)
    monkeypatch.setattr(usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(usage, "touch_last_used", lambda *a, **k: False)
    monkeypatch.setattr(scope, "group_exists", lambda _s, gid: gid == 7)
    seen = {}

    def _build(_s, gid, past_limit, wanted, now=None):
        seen.update(gid=gid, past_limit=past_limit, wanted=wanted)
        return {"group_id": gid}

    monkeypatch.setattr(events_route, "build_group_events", _build)
    return data_api.create_app().test_client(), seen


async def _get(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer dtk_1_" + "a" * 32})
    return response.status_code, await response.get_json(), response.headers


@pytest.mark.asyncio
async def test_route_defaults_and_cache(monkeypatch):
    client, seen = _client(monkeypatch)
    status, body, headers = await _get(client, "/v2/groups/7/events")
    assert status == 200 and body == {"group_id": 7}
    assert seen == {"gid": 7, "past_limit": 10, "wanted": ("upcoming", "active", "past")}
    assert headers["Cache-Control"] == "private, max-age=60"


@pytest.mark.asyncio
async def test_route_params(monkeypatch):
    client, seen = _client(monkeypatch)
    status, _, _ = await _get(client, "/v2/groups/7/events?status=upcoming,past&past_limit=0")
    assert status == 200
    assert seen["wanted"] == ("upcoming", "past") and seen["past_limit"] == 0
    for bad in ("status=soon", "status=,", "past_limit=x", "past_limit=51", "past_limit=-1"):
        status, body, _ = await _get(client, f"/v2/groups/7/events?{bad}")
        assert status == 400, bad
        assert body["error"] == "bad_request"


@pytest.mark.asyncio
async def test_route_unknown_group_and_scope(monkeypatch):
    client, _ = _client(monkeypatch)
    status, _, _ = await _get(client, "/v2/groups/8/events")
    assert status == 404
    status, _, _ = await _get(client, "/v2/groups/2/events")
    assert status == 403

    client, _ = _client(monkeypatch, key_scope="group", key_group=7)
    assert (await _get(client, "/v2/groups/7/events"))[0] == 200
    assert (await _get(client, "/v2/groups/9/events"))[0] == 403
