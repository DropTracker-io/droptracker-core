"""Event templates: optional Discord settings (suggestion #197).

``snapshot_discord`` captures the host scope; ``apply_template_discord``
re-applies it to a fresh draft only on the same (or a targetable) server and
drops channels/roles the warm bot cache no longer lists.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import web_api.routes.event_discord as edr
import web_api.routes.event_templates as etr
from web_api.common import ProblemException, abort_problem


def _real_team_discord():
    """The conftest stubs ``services``; this module is stdlib-only at import
    time, so load the real file for the config helpers."""
    path = Path(__file__).resolve().parents[2] / "services" / "event_team_discord.py"
    spec = importlib.util.spec_from_file_location("_real_event_team_discord", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _team_discord(monkeypatch):
    monkeypatch.setitem(sys.modules, "services.event_team_discord", _real_team_discord())


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)


class _S:
    def __init__(self, rows=()):
        self._rows = list(rows)
        self.added = []

    def query(self, *a, **k):
        return _Q(self._rows)

    def add(self, obj):
        self.added.append(obj)


def _event(**kw):
    base = dict(id=55, discord_guild_id="111", formation_mode="admin_assign",
                ping_config=None, discord_event_policy="on_activate",
                team_discord_config=None)
    base.update(kw)
    return SimpleNamespace(**base)


SPEC = {
    "guild_id": "111",
    "channels": {"announcements": "201", "leaderboard": "202"},
    "discord_event_policy": "immediate",
    "pings": {"event_started": ["301", "302"]},
    "team": {"channels_enabled": True, "roles_enabled": True, "voice_enabled": False,
             "forum_channel_id": None, "category_channel_id": "401",
             "retention": "keep", "captain_config": True},
}


@pytest.fixture
def cache(monkeypatch):
    """Warm bot cache for guild 111; tests edit the lists in place."""
    state = {
        "channels": [{"id": "201", "type": "text"}, {"id": "202", "type": "text"},
                     {"id": "401", "type": "category"}],
        "roles": [{"id": "301"}, {"id": "302"}],
        "targetable": True,
    }
    monkeypatch.setattr(edr, "_guild_channels", lambda gid: state["channels"])
    monkeypatch.setattr(edr, "_guild_roles", lambda gid: state["roles"])
    monkeypatch.setattr(edr, "_request_channel_refresh", lambda gid: None)

    def _target(s, uid, gid):
        if not state["targetable"]:
            abort_problem(403, "Not your server", "no")

    monkeypatch.setattr(edr, "_assert_can_target_guild", _target)
    return state


def _channels(s):
    return {c.kind: c.channel_id for c in s.added}


def test_same_server_applies_everything(cache):
    s, ev = _S(), _event()
    report = etr.apply_template_discord(s, ev, SPEC, user_id=1)
    assert report == {"applied": True, "dropped": 0}
    assert _channels(s) == {"announcements": "201", "leaderboard": "202"}
    assert all(c.group_id is None for c in s.added)
    assert ev.discord_event_policy == "immediate"
    assert json.loads(ev.ping_config) == {"event_started": ["301", "302"]}
    team = json.loads(ev.team_discord_config)
    assert team["channels_enabled"] and team["roles_enabled"]
    assert team["category_channel_id"] == "401" and team["retention"] == "keep"
    assert team["teams"] == {}


def test_no_block_is_none(cache):
    assert etr.apply_template_discord(_S(), _event(), None, user_id=1) is None
    assert etr.apply_template_discord(_S(), _event(), {"guild_id": None}, user_id=1) is None


def test_other_server_needs_target_rights(cache):
    cache["targetable"] = False
    s, ev = _S(), _event(discord_guild_id="999")
    report = etr.apply_template_discord(s, ev, SPEC, user_id=1)
    assert report["applied"] is False and "can't post" in report["reason"]
    assert s.added == [] and ev.discord_guild_id == "999" and ev.ping_config is None


def test_other_server_with_rights_repoints(cache):
    s, ev = _S(), _event(discord_guild_id=None)
    assert etr.apply_template_discord(s, ev, SPEC, user_id=1)["applied"] is True
    assert ev.discord_guild_id == "111"


def test_gone_channels_and_roles_dropped(cache):
    cache["channels"] = [{"id": "201", "type": "text"}]  # 202 + category 401 gone
    cache["roles"] = [{"id": "301"}]                     # role 302 gone
    s, ev = _S(), _event()
    report = etr.apply_template_discord(s, ev, SPEC, user_id=1)
    assert report == {"applied": True, "dropped": 3}
    assert _channels(s) == {"announcements": "201"}
    assert json.loads(ev.ping_config) == {"event_started": ["301"]}
    assert json.loads(ev.team_discord_config)["category_channel_id"] is None


def test_cold_cache_keeps_everything(cache, monkeypatch):
    monkeypatch.setattr(edr, "_guild_channels", lambda gid: None)
    monkeypatch.setattr(edr, "_guild_roles", lambda gid: None)
    s, ev = _S(), _event()
    assert etr.apply_template_discord(s, ev, SPEC, user_id=1)["dropped"] == 0
    assert len(s.added) == 2


def test_solo_event_never_enables_team_channels(cache):
    ev = _event(formation_mode="solo")
    etr.apply_template_discord(_S(), ev, SPEC, user_id=1)
    team = json.loads(ev.team_discord_config)
    assert not (team["channels_enabled"] or team["roles_enabled"] or team["voice_enabled"])


def test_snapshot_host_scope_only():
    rows = [SimpleNamespace(kind="announcements", channel_id="201", group_id=None),
            SimpleNamespace(kind="announcements", channel_id="777", group_id=9)]
    ev = _event(ping_config=json.dumps({"event_ended": ["301"]}),
                team_discord_config=json.dumps({"roles_enabled": True,
                                                "teams": {"5": {"role": True}}}))
    snap = etr.snapshot_discord(_S(rows), ev)
    assert snap["guild_id"] == "111"
    assert snap["channels"] == {"announcements": "201"}
    assert snap["pings"] == {"event_ended": ["301"]}
    assert snap["team"]["roles_enabled"] is True and "teams" not in snap["team"]
    assert etr.snapshot_discord(_S(rows), _event(discord_guild_id=None)) is None


def test_preview_has_no_ids():
    preview = etr.discord_preview(SPEC)
    assert preview == {"channels": 2, "pings": 2, "team_channels": True}
    assert "111" not in json.dumps(preview)
    assert etr.discord_preview(None) is None


def test_problem_exception_import_is_real():
    # apply_template_discord catches ProblemException from the target check.
    with pytest.raises(ProblemException):
        abort_problem(403, "x", "y")
