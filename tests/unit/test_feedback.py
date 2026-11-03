"""Clan leader feedback (web155a): answer validation, the staff Discord posts
and their buttons, and the visitor read's fail-safe.

The Discord shell (services/feedback_buttons_discord.py) is thin and is
exercised by real clicks; the decisions it relies on live in
services/feedback_core.py and are pinned here.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, _ROOT.joinpath(*parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load("_feedback_core_under_test", "services", "feedback_core.py")


# ── custom ids ───────────────────────────────────────────────────────────────

class TestCustomIds:
    @pytest.mark.parametrize("action", ["publish", "dismiss", "unpublish", "reopen"])
    def test_round_trip(self, action):
        cid = core.custom_id(action, 42)
        assert cid.startswith("dtfb:") and len(cid) <= 100
        assert core.parse_custom_id(cid) == {"action": action, "response_id": 42}

    @pytest.mark.parametrize("raw", [None, "", "dtrv:m:a:1:2", "dtfb:x:1", "dtfb:p", "dtfb:p:abc", "dtfb:p:1:2"])
    def test_rejects_anything_else(self, raw):
        assert core.parse_custom_id(raw) is None


# ── what may be published, and how it is signed ──────────────────────────────

class TestQuotable:
    def test_needs_consent_and_text(self):
        assert core.is_quotable(True, "Great bot")
        assert not core.is_quotable(False, "Great bot")
        assert not core.is_quotable(True, "   ")
        assert not core.is_quotable(True, None)

    def test_publish_button_only_when_quotable(self):
        assert [b["label"] for b in core.raw_buttons(5, True)] == ["Publish", "Dismiss"]
        assert [b["label"] for b in core.raw_buttons(5, False)] == ["Dismiss"]
        assert core.raw_buttons(5, True)[0]["custom_id"] == "dtfb:p:5"


class TestAttribution:
    def test_clan_is_the_group_name(self):
        assert core.attribution_label("clan", username="joel", group_name="Clan X") == "Clan X"

    def test_clan_without_a_group(self):
        assert core.attribution_label("clan", username="joel", group_name=None) == core.NO_CLAN_LABEL

    def test_full(self):
        assert core.attribution_label("full", username="joel", group_name="Clan X") == "joel, Clan X"
        assert core.attribution_label("full", username="joel", group_name=None) == "joel"

    def test_anonymous_hides_both(self):
        label = core.attribution_label("anon", username="joel", group_name="Clan X")
        assert label == core.ANON_LABEL
        assert "joel" not in label and "Clan X" not in label


# ── the Discord posts ────────────────────────────────────────────────────────

def _raw(**over):
    kwargs = dict(
        response_id=7, rating=8, liked_text="Loot boards", improve_text="More events",
        quote_consent=True, attribution="clan", contact_ok=True, username="joel", user_id=0,
        discord_id="123", group_name="Clan X", role="owner", tier_name="Supporter",
        member_count=1200, group_since=datetime(2026, 3, 1), source="Nov 2026 leaders",
    )
    kwargs.update(over)
    return core.raw_embed(**kwargs)


class TestRawEmbed:
    def test_carries_everything_staff_need(self):
        e = _raw()
        assert e["title"] == "8/10 · Clan X"
        assert "Owner · Supporter tier · 1,200 members · on DT since Mar 2026" in e["description"]
        # user_id 0 is a real account and must still be named
        assert "user #0" in e["description"]
        values = [f["value"] for f in e["fields"]]
        assert values[0] == "Loot boards" and values[1] == "More events"
        assert "OK to quote (Clan name only)" in values[2]
        assert "<@123>" in values[2]
        assert e["footer"]["text"] == "Response #7 · Nov 2026 leaders"

    def test_contact_only_when_allowed(self):
        e = _raw(contact_ok=False)
        assert "<@123>" not in json.dumps(e)

    def test_not_for_quoting_and_empty_answers(self):
        e = _raw(quote_consent=False, liked_text=None, improve_text="  ")
        values = [f["value"] for f in e["fields"]]
        assert values[0] == values[1] == "*Nothing written*"
        assert values[2].startswith("🚫")

    @pytest.mark.parametrize("rating,color", [(1, 0xED4245), (4, 0xED4245), (5, 0xFAA61A),
                                              (7, 0xFAA61A), (8, 0x3BA55C), (10, 0x3BA55C)])
    def test_color_follows_score(self, rating, color):
        assert _raw(rating=rating)["color"] == color

    def test_no_group(self):
        e = _raw(group_name=None, role=None, tier_name=None, member_count=None, group_since=None,
                 source=None)
        assert e["title"] == "8/10 · No clan picked"
        assert e["footer"]["text"].endswith("Share feedback page")


class TestPublishedEmbed:
    def test_only_quote_score_and_signature(self):
        e = core.published_embed(rating=9, quote="  Love it  ", attribution_text="Clan X")
        assert e["description"] == "“Love it”"
        assert e["author"]["name"] == "⭐ 9/10"
        assert e["footer"]["text"] == "Clan X"
        assert "fields" not in e

    def test_no_em_dashes(self):
        # Site copy rule: em-dashes read as AI-written.
        e = core.published_embed(rating=9, quote="Good", attribution_text="Clan X")
        assert "—" not in json.dumps(e, ensure_ascii=False)


class TestComponents:
    def test_published_has_unpublish_and_link(self):
        row = core.published_components(3, "https://discord.com/channels/1/2/3")[0]["components"]
        assert row[0]["custom_id"] == "dtfb:u:3" and row[0]["style"] == 4
        assert row[1]["style"] == 5 and row[1]["url"].startswith("https://")

    def test_published_without_link(self):
        assert len(core.published_components(3, None)[0]["components"]) == 1

    def test_dismissed_offers_reopen(self):
        assert core.dismissed_components(3)[0]["components"][0]["custom_id"] == "dtfb:r:3"

    def test_open_matches_quotability(self):
        ids = [b["custom_id"] for b in core.open_components(3, False)[0]["components"]]
        assert ids == ["dtfb:d:3"]

    def test_message_url(self):
        assert core.message_url(1, 2, 3) == "https://discord.com/channels/1/2/3"
        assert core.message_url(None, 2, 3) is None


# ── the outbox lets these buttons through ────────────────────────────────────

class _Button(SimpleNamespace):
    def __init__(self, style=None, label=None, custom_id=None, url=None):
        super().__init__(style=style, label=label, custom_id=custom_id, url=url)


class _Row:
    def __init__(self, *buttons):
        self.buttons = list(buttons)


def test_outbox_accepts_feedback_buttons(monkeypatch):
    fake = MagicMock()
    fake.Button = _Button
    fake.ActionRow = _Row
    fake.ButtonStyle = SimpleNamespace(LINK="link", GREEN="green", RED="red", GREY="grey",
                                       BLURPLE="blurple")
    monkeypatch.setitem(sys.modules, "interactions", fake)
    outbox = _load("_outbox_for_feedback", "services", "discord_outbox.py")
    rows = outbox._build_components(json.dumps(core.raw_buttons(9, True)))
    got = [(b.label, b.style, b.custom_id) for b in rows[0].buttons]
    assert got == [("Publish", "green", "dtfb:p:9"), ("Dismiss", "grey", "dtfb:d:9")]


# ── answer validation ────────────────────────────────────────────────────────

class TestValidateAnswer:
    def _v(self, **body):
        from web_api.routes.feedback import validate_answer

        return validate_answer({"rating": 8, **body})

    def test_defaults(self):
        out = self._v()
        assert out == {"rating": 8, "liked_text": None, "improve_text": None, "attribution": "clan",
                       "quote_consent": False, "contact_ok": False, "request_id": None,
                       "group_id": None}

    @pytest.mark.parametrize("rating", [0, 11, -1, None, "", True, "x"])
    def test_rating_must_be_1_to_10(self, rating):
        from web_api.common import ProblemException

        with pytest.raises(ProblemException):
            self._v(rating=rating)

    @pytest.mark.parametrize("rating", [1, 10, "7"])
    def test_rating_bounds_ok(self, rating):
        assert self._v(rating=rating)["rating"] == int(rating)

    def test_text_is_trimmed_and_capped(self):
        from web_api.common import ProblemException

        assert self._v(liked_text="  hi  ")["liked_text"] == "hi"
        assert self._v(liked_text="x" * 500)["liked_text"] == "x" * 500
        with pytest.raises(ProblemException):
            self._v(improve_text="x" * 501)

    def test_attribution_choices(self):
        from web_api.common import ProblemException

        for a in ("clan", "full", "anon"):
            assert self._v(attribution=a)["attribution"] == a
        with pytest.raises(ProblemException):
            self._v(attribution="everyone")

    def test_group_zero_is_a_real_id(self):
        assert self._v(group_id=0)["group_id"] == 0


# ── visitor read fail-safe ───────────────────────────────────────────────────

def test_live_read_failure_shows_nothing_and_is_cached(monkeypatch):
    import web_api.routes.feedback as mod

    calls = []

    class Boom:
        def __enter__(self):
            calls.append(1)
            raise RuntimeError("Table 'data.feedback_requests' doesn't exist")

        def __exit__(self, *a):
            return False

    store = {}
    monkeypatch.setattr(mod, "db_session", lambda: Boom())
    monkeypatch.setattr(mod, "cache_get", lambda k, ttl: store.get(k))
    monkeypatch.setattr(mod, "cache_set", lambda k, v: store.__setitem__(k, v))
    assert mod._live_requests() == []
    assert mod._live_requests() == []
    assert len(calls) == 1


def test_global_group_is_never_offered(monkeypatch):
    import web_api.popup_audience as pa
    import web_api.routes.feedback as mod

    monkeypatch.setattr(pa, "_leader_roles", lambda s, uid: {2: "owner"})
    assert mod.leader_groups(MagicMock(), 0) == []
