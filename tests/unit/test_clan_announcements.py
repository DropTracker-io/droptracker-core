"""Group announcements sent in game, and the Discord embed options.

A clan admin's announcement can also land in every member's plugin inbox as a
``clan_announcement`` envelope, which plugins 6.1.12+ render from its
``display`` block (a chat line plus a pop-up). These tests pin the envelope the
plugin reads, who receives it, and the route's validation of the new fields.
"""
import importlib.util
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(_ROOT, *path_parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pn = _load("_plugin_notifications_clan_ann_ut", "services", "plugin_notifications.py")
display = _load("_plugin_display_clan_ann_ut", "services", "plugin_display.py")

import web_api.routes.announcements as ann_routes  # noqa: E402


class _Pipe:
    def __init__(self, log):
        self.log = log

    def rpush(self, key, value):
        self.log.append(("rpush", key, value))

    def ltrim(self, key, start, end):
        self.log.append(("ltrim", key, start, end))

    def expire(self, key, ttl):
        self.log.append(("expire", key, ttl))

    def publish(self, channel, message):
        self.log.append(("publish", channel, message))

    def execute(self):
        return []


class _Redis:
    def __init__(self):
        self.log = []
        self.pipelines = 0

    def pipeline(self, transaction=True):
        self.pipelines += 1
        return _Pipe(self.log)


@pytest.fixture(autouse=True)
def _real_display(monkeypatch):
    # The conftest stubs the services package; the envelope builder lazy-
    # imports plugin_display for its sanitizer.
    monkeypatch.setitem(sys.modules, "services.plugin_display", display)


class TestEnvelope:
    def test_display_is_what_the_plugin_renders(self):
        env = pn.clan_announcement_envelope(
            41, 306, "AstralStar", "Bingo starts Friday", "Sign up on the site!", now=1000)
        assert env["type"] == "clan_announcement"
        assert env["priority"] == "high"  # past "important pop-ups only"
        assert "event" not in env  # event-less: a plain [DropTracker] chat line
        d = env["display"]
        assert d["v"] == 1
        assert d["title"] == "Bingo starts Friday"
        assert d["text"] == "AstralStar: Sign up on the site!"
        assert d["toast"] is True
        assert d["card"] is None

    def test_each_announcement_has_its_own_popup_key(self):
        # The plugin keys pop-up dedupe on event|task|type; without a task
        # label two announcements 10s apart would collapse into one.
        a = pn.clan_announcement_envelope(1, 306, "A", "t", "m")
        b = pn.clan_announcement_envelope(2, 306, "A", "t", "m")
        assert a["data"]["task_label"] != b["data"]["task_label"]
        assert "task_id" not in a["data"]  # task_id would hit the hidden-task filter

    def test_markup_is_stripped_and_text_capped(self):
        env = pn.clan_announcement_envelope(
            1, 306, "<col=ff0000>Clan</col>", "<img=1>Hi", "x" * 500)
        d = env["display"]
        assert d["title"] == "Hi"
        assert d["text"].startswith("Clan: ")
        # Within the plugin's own 240-character text cap.
        assert len(d["text"]) <= 240

    def test_message_falls_back_to_title(self):
        env = pn.clan_announcement_envelope(1, 306, "Clan", "Title only", None)
        assert env["display"]["text"] == "Clan: Title only"

    def test_missing_group_name(self):
        env = pn.clan_announcement_envelope(1, 306, None, "T", "m")
        assert env["display"]["text"] == "Your clan: m"


class TestPush:
    @pytest.fixture
    def wired(self, monkeypatch):
        fake = _Redis()
        monkeypatch.setattr(pn, "_redis", lambda: fake)
        monkeypatch.setattr(pn, "group_member_player_ids", lambda s, gid: {1, 2, 3})
        monkeypatch.setattr(pn, "_players_with_type_disabled", lambda s, t, ids: {2})
        return fake

    def _push(self, group_id=306):
        return pn.push_clan_announcement(
            object(), announcement_id=9, group_id=group_id, group_name="AstralStar",
            title="T", message="m")

    def test_every_member_but_the_opted_out(self, wired):
        assert self._push() == 2
        pushed = {op[1] for op in wired.log if op[0] == "rpush"}
        assert pushed == {"plugin:notify:1", "plugin:notify:3"}
        woken = {op[2] for op in wired.log if op[0] == "publish"}
        assert woken == {"1", "3"}

    def test_one_round_trip_for_the_clan(self, wired):
        self._push()
        assert wired.pipelines == 1

    def test_inbox_cap_and_ttl_kept(self, wired):
        self._push()
        assert ("ltrim", "plugin:notify:1", -pn.INBOX_CAP, -1) in wired.log
        assert ("expire", "plugin:notify:1", pn.INBOX_TTL_SECONDS) in wired.log

    def test_envelope_on_the_wire(self, wired):
        self._push()
        raw = next(op[2] for op in wired.log if op[0] == "rpush")
        env = json.loads(raw)
        assert env["type"] == "clan_announcement"
        assert env["data"]["announcement_id"] == 9

    def test_global_group_never_fans_out(self, wired):
        assert self._push(group_id=pn.GLOBAL_GROUP_ID) == 0
        assert wired.log == []

    def test_redis_failure_is_swallowed(self, monkeypatch):
        def boom():
            raise RuntimeError("down")

        monkeypatch.setattr(pn, "_redis", boom)
        monkeypatch.setattr(pn, "group_member_player_ids", lambda s, gid: {1})
        monkeypatch.setattr(pn, "_players_with_type_disabled", lambda s, t, ids: set())
        assert self._push() == 0

    def test_pref_is_listed_for_players(self):
        assert "clan_announcement" in pn.PLAYER_PREF_TYPES
        # Not an event type: it never goes through event fan-out.
        assert "clan_announcement" not in pn.WEB_PREF_TYPES
        assert "clan_announcement" not in pn.AUDIENCE_FOR_TYPE


class _Abort(Exception):
    pass


@pytest.fixture
def aborts(monkeypatch):
    def _abort(status, title, detail=None):
        raise _Abort(status, title)

    monkeypatch.setattr(ann_routes, "abort_problem", _abort)
    monkeypatch.setitem(sys.modules, "services.plugin_notifications", pn)


class TestInGameValidation:
    def test_off_by_default(self, aborts):
        assert ann_routes._validate_in_game({}, "Title") == (False, None)

    def test_defaults_to_title(self, aborts):
        assert ann_routes._validate_in_game({"send_in_game": True}, "Title") == (True, "Title")

    def test_one_line(self, aborts):
        _, msg = ann_routes._validate_in_game(
            {"send_in_game": True, "in_game_message": "  two\nlines  "}, "T")
        assert msg == "two lines"

    def test_too_long(self, aborts):
        with pytest.raises(_Abort):
            ann_routes._validate_in_game(
                {"send_in_game": True, "in_game_message": "x" * 201}, "T")


class TestEmbedStyle:
    def test_defaults(self, aborts):
        style = ann_routes._validate_embed_style({})
        assert style["color"] == 0xC8AA6E
        assert style["url"] is None and style["thumbnail"] is None
        assert style["footer"] is None and style["timestamp"] is False

    def test_bad_colour(self, aborts):
        with pytest.raises(_Abort):
            ann_routes._validate_embed_style({"embed_color": "red"})

    def test_non_http_link(self, aborts):
        with pytest.raises(_Abort):
            ann_routes._validate_embed_style({"embed_url": "javascript:alert(1)"})

    def test_full_embed(self, aborts):
        from datetime import datetime

        style = ann_routes._validate_embed_style({
            "embed_color": "#ff0000", "embed_url": "https://example.com/x",
            "embed_thumbnail_url": "https://example.com/t.png",
            "embed_footer": "AstralStar staff", "embed_timestamp": True,
        })
        embed = ann_routes._discord_embed(
            "T", "body", "https://example.com/i.png", style, datetime(2026, 11, 12, 10, 0))
        assert embed["color"] == 0xFF0000
        assert embed["url"] == "https://example.com/x"
        assert embed["image"] == {"url": "https://example.com/i.png"}
        assert embed["thumbnail"] == {"url": "https://example.com/t.png"}
        assert embed["footer"] == {"text": "AstralStar staff"}
        assert embed["timestamp"].startswith("2026-11-12T10:00:00")

    def test_minimal_embed_unchanged(self, aborts):
        from datetime import datetime

        embed = ann_routes._discord_embed(
            "T", "b" * 5000, None, ann_routes._validate_embed_style({}), datetime.now())
        assert embed == {"title": "T", "description": "b" * 4000, "color": 0xC8AA6E}
