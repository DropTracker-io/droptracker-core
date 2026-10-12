"""The submission API (suggestion #198), from the public route to the engine.

Covers the pieces that decide what a call does without a database:

* the route's body parsing, image checks and intake-answer mapping
  (``data_api/routes/submissions.py``), plus the route end to end with the
  key, member lookup and intake forward stubbed;
* the intake helpers (``data/submissions/api_submissions.py``): context
  parsing, the duplicate-check window, count-aware matching, trust stamping;
* trust plumbing: ``common.trusted_*``, the manual policy's trusted groups,
  the engine's per-event plugin check, and the client-field strip;
* key flags in ``db/api_keys.py``.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, *parts: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT.joinpath(*parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


keys = _load("_submit_api_keys", "db", "api_keys.py")
engine = _load("_submit_event_engine", "services", "event_engine.py")
mp = _load("_submit_manual_policy", "data", "submissions", "manual_policy.py")


# ── route: pure parsing ──────────────────────────────────────────────────────

class TestBuildIntakePayload:
    def _build(self, **body):
        from data_api.routes import submissions as r
        return r.build_intake_payload(body)

    def test_drop_maps_to_the_intake_fields(self):
        sub_type, payload = self._build(type="drop", item="Abyssal whip",
                                        npc="Abyssal demon", quantity="1", kc=12)
        assert sub_type == "drop"
        assert payload == {"submission_type": "drop", "world_type": "main",
                           "item_name": "Abyssal whip", "npc_name": "Abyssal demon",
                           "quantity": 1, "kill_count": 12}

    def test_drop_needs_item_and_npc(self):
        from data_api.routes import submissions as r
        with pytest.raises(r.SubmissionError) as err:
            self._build(type="drop", item="Abyssal whip")
        assert err.value.status == 422

    def test_unknown_type_is_refused(self):
        from data_api.routes import submissions as r
        with pytest.raises(r.SubmissionError):
            self._build(type="quest")

    def test_pb_accepts_clock_text(self):
        _t, payload = self._build(type="pb", npc="Vorkath", time="1:02.4", team_size=1)
        assert payload["time_ms"] == 62400
        assert payload["boss_name"] == "Vorkath"

    def test_pb_accepts_raw_ms(self):
        _t, payload = self._build(type="pb", npc="Vorkath", time_ms=62400)
        assert payload["time_ms"] == 62400

    def test_ca_needs_a_real_tier(self):
        from data_api.routes import submissions as r
        with pytest.raises(r.SubmissionError):
            self._build(type="ca", task="Faithless Crypt Run", tier="legendary")
        _t, payload = self._build(type="ca", task="Faithless Crypt Run", tier="Elite")
        assert payload["tier"] == "elite"

    def test_pet_and_clog_use_their_processor_keys(self):
        _t, pet = self._build(type="pet", item="Pet chaos elemental", npc="Chaos Elemental", kc="40")
        assert pet["pet_name"] == "Pet chaos elemental" and pet["killcount"] == 40
        _t, clog = self._build(type="clog", item="Dragon pickaxe", npc="Callisto")
        assert clog["item_name"] == "Dragon pickaxe" and clog["source"] == "Callisto"

    def test_negative_quantity_is_refused(self):
        from data_api.routes import submissions as r
        with pytest.raises(r.SubmissionError):
            self._build(type="drop", item="Coins", npc="Goblin", quantity=0)


class TestParseTimeMs:
    @pytest.mark.parametrize("raw,ms", [
        ("83.4", 83400), ("1:23.4", 83400), ("1:02:03", 3723000), (90000, 90000), ("90000", 90000),
    ])
    def test_formats(self, raw, ms):
        from data_api.routes import submissions as r
        assert r.parse_time_ms(raw) == ms

    @pytest.mark.parametrize("raw", ["abc", "1:2:3:4", "-1:00", True])
    def test_garbage(self, raw):
        from data_api.routes import submissions as r
        with pytest.raises(r.SubmissionError):
            r.parse_time_ms(raw)


class TestApiFields:
    def test_defaults(self):
        from data_api.routes import submissions as r
        fields = r.parse_api_fields({})
        assert fields == {"api_external_id": None, "api_note": None, "api_skip_if_tracked": True}

    def test_milliseconds_obtained_at_is_folded_to_seconds(self):
        from data_api.routes import submissions as r
        assert r.parse_api_fields({"obtained_at": 1760270400123})["api_obtained_at"] == 1760270400

    def test_skip_if_tracked_can_be_turned_off(self):
        from data_api.routes import submissions as r
        assert r.parse_api_fields({"skip_if_tracked": "false"})["api_skip_if_tracked"] is False
        with pytest.raises(r.SubmissionError):
            r.parse_api_fields({"skip_if_tracked": "maybe"})


class TestImageChecks:
    @pytest.mark.parametrize("url", [
        "https://cdn.discordapp.com/attachments/1/2/proof.png?ex=1",
        "https://media.discordapp.net/attachments/1/2/proof.png",
    ])
    def test_discord_cdn_is_allowed(self, url):
        from data_api.routes import submissions as r
        assert r.check_image_url(url) == url

    @pytest.mark.parametrize("url", [
        "http://cdn.discordapp.com/a.png",            # not https
        "https://evil.example/a.png",                 # wrong host
        "https://cdn.discordapp.com.evil.example/a",  # suffix trick
        "https://127.0.0.1/a.png",
        "https://cdn.discordapp.com:8443/a.png",      # odd port
        "file:///etc/passwd",
    ])
    def test_everything_else_is_refused(self, url):
        from data_api.routes import submissions as r
        with pytest.raises(r.SubmissionError):
            r.check_image_url(url)

    def _png(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (200, 10, 10)).save(buf, format="PNG")
        return buf.getvalue()

    def test_real_png_is_accepted_by_its_bytes(self):
        from data_api.routes import submissions as r
        assert r.check_image_bytes(self._png()) == ("png", "image/png")

    def test_non_image_is_refused(self):
        from data_api.routes import submissions as r
        with pytest.raises(r.SubmissionError):
            r.check_image_bytes(b"<html>not an image</html>")
        with pytest.raises(r.SubmissionError):
            r.check_image_bytes(b"")


class TestIntakeResponse:
    def test_accepted(self):
        from data_api.routes import submissions as r
        status, body = r.intake_response(200, {"success": True, "status": "accepted",
                                               "api_submission_id": 9, "message": "ok"})
        assert status == 200 and body["id"] == 9 and body["status"] == "accepted"

    def test_already_tracked_is_a_success(self):
        from data_api.routes import submissions as r
        status, body = r.intake_response(200, {"status": "already_tracked", "matched_drop_id": 77})
        assert status == 200 and body["matched_drop_id"] == 77

    def test_processor_rejection_is_422(self):
        from data_api.routes import submissions as r
        status, body = r.intake_response(200, {"success": False, "status": "rejected",
                                               "message": "Unknown item"})
        assert status == 422 and body["detail"] == "Unknown item"

    def test_intake_validation_error_is_422(self):
        from data_api.routes import submissions as r
        status, body = r.intake_response(400, {"success": False, "error": "bad"})
        assert status == 422 and body["detail"] == "bad"

    def test_replay_of_an_unfinished_call_is_409(self):
        from data_api.routes import submissions as r
        status, body = r.intake_response(200, {"status": "processing", "replayed": True,
                                               "api_submission_id": 4})
        assert status == 409 and body["error"] == "in_progress" and body["id"] == 4

    @pytest.mark.parametrize("code", [401, 503, 500])
    def test_our_own_failures_are_502(self, code):
        from data_api.routes import submissions as r
        status, _body = r.intake_response(code, {})
        assert status == 502


# ── route: end to end with the edges stubbed ─────────────────────────────────

class _Decision:
    allowed = True
    reason = ""
    retry_after = None
    headers = {"X-RateLimit-Cost": "50"}


class _Slot:
    acquired = True

    def __init__(self, *_a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _key(**overrides):
    key = {
        "key_id": 3, "label": "clan bot", "tier": "standard", "scope": "group",
        "owner_type": "group", "owner_user_id": None, "group_id": 7,
        "limits": {"requests_per_min": 100, "cost_units_per_min": 10 ** 9,
                   "requests_per_day": 10 ** 6, "max_concurrency": 4},
        "can_submit": True, "submit_preapproved": True,
    }
    key.update(overrides)
    return key


@pytest.fixture
def route(monkeypatch):
    import data_api
    import data_api.auth as auth
    import data_api.usage as usage
    from data_api.routes import submissions as r
    from quart import g

    state = {"key": _key(), "forwarded": [], "members": {"Koeppy": (2, "Koeppy")}}

    async def _authenticate():
        g.api_key = state["key"]
        return None

    async def _forward(payload, image):
        state["forwarded"].append((payload, image))
        return 200, {"success": True, "status": "accepted", "api_submission_id": 41,
                     "message": "Drop processed"}

    monkeypatch.setattr(auth, "authenticate_request", _authenticate)
    monkeypatch.setattr(r, "check_and_charge", lambda *_a: _Decision())
    monkeypatch.setattr(r, "_submit_rate_ok", lambda _k: True)
    monkeypatch.setattr(r, "Concurrency", _Slot)
    monkeypatch.setattr(r, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(r, "_resolve_member",
                        lambda _s, gid, ref: state["members"].get(str(ref)) if gid == 7 else None)
    monkeypatch.setattr(r, "_forward", _forward)
    monkeypatch.setattr(usage, "record", lambda *a, **k: None)
    return data_api.create_app().test_client(), state


async def _post(client, body):
    response = await client.post("/v2/submissions", json=body,
                                 headers={"Authorization": "Bearer dtk_3_" + "a" * 32})
    return response.status_code, await response.get_json()


_WHIP = {"type": "drop", "player": "Koeppy", "item": "Abyssal whip", "npc": "Abyssal demon"}


class TestRoute:
    @pytest.mark.asyncio
    async def test_happy_path_forwards_with_the_key_context(self, route):
        client, state = route
        status, body = await _post(client, {**_WHIP, "external_id": "msg-1", "note": "staff ok"})
        assert status == 200
        assert body["id"] == 41 and body["status"] == "accepted" and body["preapproved"] is True
        payload, image = state["forwarded"][0]
        assert image is None
        assert payload["player_name"] == "Koeppy"
        assert payload["api_key_id"] == 3 and payload["api_group_id"] == 7
        assert payload["api_preapproved"] is True
        assert payload["api_external_id"] == "msg-1" and payload["api_note"] == "staff ok"
        assert payload["api_skip_if_tracked"] is True

    @pytest.mark.asyncio
    async def test_read_only_key_is_forbidden(self, route):
        client, state = route
        state["key"] = _key(can_submit=False, submit_preapproved=False)
        status, body = await _post(client, _WHIP)
        assert status == 403 and body["error"] == "forbidden"
        assert state["forwarded"] == []

    @pytest.mark.asyncio
    async def test_global_key_is_forbidden_even_with_the_flag(self, route):
        client, state = route
        state["key"] = _key(scope="global", group_id=None)
        status, _ = await _post(client, _WHIP)
        assert status == 403

    @pytest.mark.asyncio
    async def test_non_member_is_404_and_nothing_is_forwarded(self, route):
        client, state = route
        status, body = await _post(client, {**_WHIP, "player": "Zezima"})
        assert status == 404 and body["error"] == "not_found"
        assert state["forwarded"] == []

    @pytest.mark.asyncio
    async def test_non_discord_image_url_is_refused_before_any_fetch(self, route):
        client, state = route
        status, body = await _post(client, {**_WHIP, "image_url": "https://evil.example/x.png"})
        assert status == 422 and "Discord" in body["detail"]
        assert state["forwarded"] == []

    @pytest.mark.asyncio
    async def test_player_id_zero_is_a_real_id(self, route):
        client, state = route
        state["members"]["0"] = (0, "Player Zero")
        body = {k: v for k, v in _WHIP.items() if k != "player"}
        status, out = await _post(client, {**body, "player_id": 0})
        assert status == 200 and out["player"] == "Player Zero"

    @pytest.mark.asyncio
    async def test_missing_player_is_422(self, route):
        client, _ = route
        status, _ = await _post(client, {k: v for k, v in _WHIP.items() if k != "player"})
        assert status == 422

    @pytest.mark.asyncio
    async def test_not_preapproved_key_says_so(self, route):
        client, state = route
        state["key"] = _key(submit_preapproved=False)
        status, body = await _post(client, _WHIP)
        assert status == 200 and body["preapproved"] is False
        assert state["forwarded"][0][0]["api_preapproved"] is False

    @pytest.mark.asyncio
    async def test_rate_limit_ceiling(self, route, monkeypatch):
        from data_api.routes import submissions as r
        client, state = route
        monkeypatch.setattr(r, "_submit_rate_ok", lambda _k: False)
        status, body = await _post(client, _WHIP)
        assert status == 429 and body["limit"] == "submissions_per_min"
        assert state["forwarded"] == []


# ── intake helpers ───────────────────────────────────────────────────────────

class TestParseApiContext:
    def test_non_api_calls_have_no_context(self):
        from data.submissions import api_submissions as a
        assert a.parse_api_context({"player_name": "x"}) is None
        assert a.parse_api_context({"api_key_id": True, "api_group_id": 7}) is None

    def test_multipart_coercions(self):
        from data.submissions import api_submissions as a
        ctx = a.parse_api_context({
            "api_key_id": 3, "api_group_id": 7, "api_preapproved": "true",
            "api_external_id": 123456, "api_note": 42, "api_obtained_at": 1760270400,
        })
        assert ctx["key_id"] == 3 and ctx["group_id"] == 7 and ctx["preapproved"] is True
        assert ctx["external_id"] == "123456" and ctx["note"] == "42"
        assert ctx["obtained_at"] == datetime(2025, 10, 12, 12, 0)
        assert ctx["skip_if_tracked"] is True  # on by default

    def test_skip_if_tracked_false(self):
        from data.submissions import api_submissions as a
        ctx = a.parse_api_context({"api_key_id": 3, "api_group_id": 7,
                                   "api_skip_if_tracked": False})
        assert ctx["skip_if_tracked"] is False and ctx["preapproved"] is False


class TestTrackedWindow:
    def test_default_is_the_last_12_hours(self):
        from data.submissions import api_submissions as a
        now = datetime(2026, 10, 12, 12)
        assert a.tracked_window(None, now) == (now - timedelta(hours=12), now)

    def test_obtained_at_narrows_to_half_an_hour_each_side(self):
        from data.submissions import api_submissions as a
        now = datetime(2026, 10, 12, 12)
        t = datetime(2026, 10, 12, 9)
        assert a.tracked_window(t, now) == (t - timedelta(minutes=30), t + timedelta(minutes=30))

    def test_obtained_at_far_in_the_future_is_ignored(self):
        from data.submissions import api_submissions as a
        now = datetime(2026, 10, 12, 12)
        start, end = a.tracked_window(now + timedelta(days=1), now)
        assert end == now


class TestPickUnmatched:
    def test_one_tracked_whip_covers_one_submission_only(self):
        from data.submissions import api_submissions as a
        # First screenshot: the plugin's whip stands in for it.
        assert a.pick_unmatched([500], []) == 500
        # Second screenshot of another whip: that tracked whip is taken, so
        # this one is new and gets recorded.
        assert a.pick_unmatched([500], [500]) is None

    def test_two_tracked_whips_cover_two(self):
        from data.submissions import api_submissions as a
        assert a.pick_unmatched([700, 500], [500]) == 700

    def test_oldest_first(self):
        from data.submissions import api_submissions as a
        assert a.pick_unmatched([900, 300, 600], []) == 300


class TestApplyTrust:
    def test_preapproved_stamps_group_and_events(self):
        from data.submissions import api_submissions as a
        from data.submissions.common import API_TRUSTED_EVENTS_KEY, API_TRUSTED_GROUPS_KEY
        data = {}
        a.apply_trust(data, {"preapproved": True, "group_id": 7}, [125, 19, 125])
        assert data[API_TRUSTED_GROUPS_KEY] == [7]
        assert data[API_TRUSTED_EVENTS_KEY] == [19, 125]

    def test_not_preapproved_stamps_nothing(self):
        from data.submissions import api_submissions as a
        data = {}
        a.apply_trust(data, {"preapproved": False, "group_id": 7}, [125])
        assert data == {}


class TestOutcomeBody:
    def test_shapes(self):
        from data.submissions import api_submissions as a
        row = SimpleNamespace(id=5, status="already_tracked", message="m",
                              submission_guid=None, matched_drop_id=99)
        body = a.outcome_body(row, replayed=True)
        assert body == {"success": True, "status": "already_tracked", "message": "m",
                        "api_submission_id": 5, "guid": None, "matched_drop_id": 99,
                        "replayed": True}
        assert a.outcome_body(SimpleNamespace(id=6, status="rejected"))["success"] is False


# ── trust plumbing ───────────────────────────────────────────────────────────

class TestCommonTrust:
    def test_only_manual_submissions_carry_trust(self):
        from data.submissions.common import trusted_event_ids, trusted_group_ids
        manual = {"intake_source": "manual", "_api_trusted_group_ids": [7, "8", True, "x"],
                  "_api_trusted_event_ids": [125]}
        assert trusted_group_ids(manual) == {7, 8}
        assert trusted_event_ids(manual) == [125]
        plugin = {"_api_trusted_group_ids": [7], "_api_trusted_event_ids": [125]}
        assert trusted_group_ids(plugin) == set()
        assert trusted_event_ids(plugin) == []

    def test_client_cannot_supply_the_markers(self):
        from utils import replay_stamp
        data = {"item_name": "Twisted bow", "_api_trusted_group_ids": "[7]",
                "_api_trusted_event_ids": "[125]", "_received_at": 1}
        assert replay_stamp.strip_client_stamp_fields(data) == {"item_name": "Twisted bow"}


class TestManualPolicyTrust:
    @pytest.fixture
    def policies(self, monkeypatch):
        values = {}
        fake = types.ModuleType("utils.group_config")
        fake.get_bulk = lambda _s, gids, cfg_keys: {
            (g, k): values.get(g) for g in gids for k in cfg_keys}
        import utils
        monkeypatch.setitem(sys.modules, "utils.group_config", fake)
        monkeypatch.setattr(utils, "group_config", fake, raising=False)
        monkeypatch.setattr(mp, "_authorized_group_ids", lambda *_a: set())
        return values

    def test_trusted_group_skips_its_own_review_only(self, policies):
        policies.update({7: "confirm", 9: "confirm"})
        player = SimpleNamespace(user_id=55)
        held = mp.manual_moderation_for_player(None, player, [7, 9], trusted_group_ids={7})
        assert held == {9: ("pending", "confirm")}

    def test_block_still_blocks_a_trusted_group(self, policies):
        policies.update({7: "block"})
        held = mp.manual_moderation_for_player(None, SimpleNamespace(user_id=1), [7],
                                               trusted_group_ids={7})
        assert held == {7: ("excluded", "block")}

    def test_without_trust_nothing_changes(self, policies):
        policies.update({7: "confirm"})
        held = mp.manual_moderation_for_player(None, SimpleNamespace(user_id=1), [7])
        assert held == {7: ("pending", "confirm")}


def _event(**kw):
    ev = {"id": 125, "group_id": 7, "requires_confirmation": False,
          "submission_policy": "confirm_non_api"}
    ev.update(kw)
    return ev


def _task(**kw):
    task = {"id": 1, "type": "item_collection", "requires_confirmation": False,
            "config": None, "target": "Abyssal whip"}
    task.update(kw)
    return task


class TestEngineTrust:
    def test_trusted_event_counts_as_plugin(self):
        env = {"used_api": False, "trusted_event_ids": [125]}
        assert engine.envelope_from_plugin_for(_event(), env) is True
        assert engine.completion_status(_event(), _task(), env) == "auto"

    def test_trust_is_per_event(self):
        env = {"used_api": False, "trusted_event_ids": [125]}
        other = _event(id=126, group_id=9)
        assert engine.envelope_from_plugin_for(other, env) is False
        assert engine.completion_status(other, _task(), env) == "pending"

    def test_api_only_accepts_a_trusted_manual_submission(self):
        env = {"used_api": False, "trusted_event_ids": [125]}
        assert engine.accepts_submission_source(_event(submission_policy="api_only"), env) is True
        assert engine.accepts_submission_source(
            _event(submission_policy="api_only"), {"used_api": False}) is False

    def test_explicit_confirmation_still_holds_a_trusted_submission(self):
        env = {"used_api": False, "trusted_event_ids": [125]}
        assert engine.completion_status(
            _event(requires_confirmation=True), _task(), env) == "pending"
        assert engine.completion_status(
            _event(), _task(requires_confirmation=True), env) == "pending"

    def test_untrusted_manual_unchanged(self):
        assert engine.completion_status(_event(), _task(), {"used_api": False}) == "pending"
        assert engine.completion_status(_event(), _task(), {"used_api": True}) == "auto"

    def test_garbage_trust_list_is_not_trust(self):
        env = {"used_api": False, "trusted_event_ids": ["x"]}
        assert engine.envelope_from_plugin_for(_event(), env) is False


class _Redis:
    def __init__(self):
        self.lists = {}

    def exists(self, key):
        return 1 if key == engine.ACTIVE_EVENTS_KEY else 0

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)


class TestQueueSubmissionTrust:
    @pytest.fixture
    def redis(self, monkeypatch):
        import utils.redis as ur
        r = _Redis()
        monkeypatch.setattr(ur, "redis_client", type("RC", (), {"client": r})(), raising=False)
        import utils.mirror_context as mc
        monkeypatch.setattr(mc, "skip_mirrored_extras", lambda: False, raising=False)
        return r

    def test_trusted_ids_ride_on_the_envelope(self, redis):
        engine.queue_submission("drop", 5, "g", {"item_name": "Abyssal whip"},
                                used_api=False, trusted_event_ids=[125])
        envelope = json.loads(redis.lists[engine.QUEUE_KEY][0])
        assert envelope["trusted_event_ids"] == [125] and envelope["used_api"] is False

    def test_absent_trust_leaves_the_envelope_as_before(self, redis):
        engine.queue_submission("drop", 5, "g", {"item_name": "Abyssal whip"}, used_api=True)
        envelope = json.loads(redis.lists[engine.QUEUE_KEY][0])
        assert "trusted_event_ids" not in envelope


# ── key flags ────────────────────────────────────────────────────────────────

def _row(**overrides):
    defaults = dict(id=3, token_hash="", token_prefix="", label="bot", scope="group",
                    owner_user_id=None, group_id=7, tier_key="standard",
                    requests_per_min=None, cost_units_per_min=None, requests_per_day=None,
                    max_concurrency=None, revoked_at=None, expires_at=None,
                    can_submit=True, submit_preapproved=True)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestKeyFlags:
    def test_group_key_flags_pass_through(self):
        d = keys.key_descriptor(_row(), None)
        assert d["can_submit"] is True and d["submit_preapproved"] is True

    def test_preapproved_without_submit_is_nothing(self):
        d = keys.key_descriptor(_row(can_submit=False), None)
        assert d["can_submit"] is False and d["submit_preapproved"] is False

    def test_non_group_key_never_submits(self):
        d = keys.key_descriptor(_row(scope="global", group_id=None), None)
        assert d["can_submit"] is False and d["submit_preapproved"] is False

    def test_legacy_row_without_columns_is_read_only(self):
        row = _row()
        del row.can_submit, row.submit_preapproved
        d = keys.key_descriptor(row, None)
        assert d["can_submit"] is False and d["submit_preapproved"] is False

    def test_create_key_refuses_submit_on_a_user_key(self):
        with pytest.raises(ValueError):
            keys.create_key(None, owner_user_id=1, can_submit=True)

    def test_create_key_refuses_preapproved_without_submit(self):
        with pytest.raises(ValueError):
            keys.create_key(None, group_id=7, submit_preapproved=True)
