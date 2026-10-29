"""Discord delivery relay (web153a): the outbox protocol, payload rewriting,
interaction validation, and the core bot's stand-in channel/message objects.

``services/discord_relay_core.py`` is stdlib-only and is driven here against
an in-memory Redis with the same bytes-out behaviour as the real client.
``services/discord_relay.py`` is loaded with the real core wired in, and its
RelayChannel/RelayMessage are exercised with real ``interactions`` components
so the queued body is exactly what the library would have sent.
"""
from __future__ import annotations

import asyncio
import fnmatch
import importlib.util
import re
import sys
import time
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, _ROOT.joinpath(*parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load("_relay_core_under_test", "services", "discord_relay_core.py")


# ── an in-memory Redis (bytes out, like redis-py without decode_responses) ──

class FakeRedis:
    def __init__(self):
        self.kv, self.z, self.h, self.l = {}, {}, {}, {}

    @staticmethod
    def _b(v):
        if isinstance(v, bytes):
            return v
        return str(v).encode()

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, ex=None):
        self.kv[k] = self._b(v)
        return True

    def delete(self, *ks):
        n = 0
        for k in ks:
            for store in (self.kv, self.z, self.h, self.l):
                if k in store:
                    del store[k]
                    n += 1
        return n

    def incr(self, k, amount=1):
        v = int(self.kv.get(k, b"0")) + amount
        self.kv[k] = self._b(v)
        return v

    def expire(self, k, s):
        return True

    def zadd(self, k, mapping):
        d = self.z.setdefault(k, {})
        for m, score in mapping.items():
            d[self._b(m)] = float(score)
        return len(mapping)

    def zrem(self, k, *ms):
        d = self.z.get(k, {})
        n = 0
        for m in ms:
            if d.pop(self._b(m), None) is not None:
                n += 1
        return n

    def zrangebyscore(self, k, lo, hi):
        lo = float("-inf") if lo == "-inf" else float(lo)
        hi = float("inf") if hi == "+inf" else float(hi)
        d = self.z.get(k, {})
        return [m for m, s in sorted(d.items(), key=lambda x: x[1]) if lo <= s <= hi]

    def zcard(self, k):
        return len(self.z.get(k, {}))

    def hset(self, k, field, value):
        self.h.setdefault(k, {})[self._b(field)] = self._b(value)
        return 1

    def hincrby(self, k, field, amount=1):
        d = self.h.setdefault(k, {})
        v = int(d.get(self._b(field), b"0")) + amount
        d[self._b(field)] = self._b(v)
        return v

    def hgetall(self, k):
        return dict(self.h.get(k, {}))

    def lpush(self, k, *vs):
        lst = self.l.setdefault(k, [])
        for v in vs:
            lst.insert(0, self._b(v))
        return len(lst)

    def ltrim(self, k, start, stop):
        lst = self.l.get(k, [])
        self.l[k] = lst[start:stop + 1]
        return True

    def lrange(self, k, start, stop):
        lst = self.l.get(k, [])
        return lst[start:stop + 1 if stop >= 0 else None]

    def brpop(self, k, timeout=0):
        lst = self.l.get(k, [])
        if not lst:
            return None
        return (self._b(k), lst.pop())

    def keys(self, pattern):
        return [k for k in self.kv if fnmatch.fnmatch(k, pattern)]


IID = 7
T0 = 1_700_000_000.0


@pytest.fixture
def r():
    return FakeRedis()


def _create(r, body=None, files=None, now=T0, iid=IID, channel="111"):
    return core.enqueue_create(r, iid, channel, body or {"content": "hi"}, files, "test", now=now)


# ── synthetic ids ──────────────────────────────────────────────────────────

class TestSyntheticIds:
    def test_round_trip(self):
        sid = core.synthetic_id(42)
        assert core.is_relay_message_id(sid)
        assert core.is_relay_message_id(str(sid))
        assert core.relay_mid(str(sid)) == 42

    def test_real_snowflakes_are_not_relay_ids(self):
        assert not core.is_relay_message_id("1413573204886294599")
        assert not core.is_relay_message_id(None)
        assert not core.is_relay_message_id("abc")

    def test_relay_ids_sort_after_real_messages_and_by_age(self):
        # event_lootboard_post._below compares ids as ints to decide which
        # message is lower in the channel — relay ids must keep that true.
        real = 1_413_573_204_886_294_599
        assert core.synthetic_id(1) > real
        assert core.synthetic_id(2) > core.synthetic_id(1)

    def test_fits_a_signed_bigint(self):
        assert core.synthetic_id(10 ** 12) < 2 ** 63


# ── outbox ──────────────────────────────────────────────────────────────────

class TestClaimAndAck:
    def test_create_is_delivered_once_then_acked(self, r):
        mid = _create(r)
        batch = core.claim(r, IID, now=T0 + 1)
        assert [m["id"] for m in batch] == [mid]
        assert batch[0]["op"] == "create" and batch[0]["body"] == {"content": "hi"}
        assert core.claim(r, IID, now=T0 + 2) == []  # leased, not re-handed out
        assert core.ack(r, IID, mid, ok=True, discord_message_id="9001", now=T0 + 3) == \
            {"status": "sent"}
        assert core.load(r, mid)["discord_message_id"] == "9001"
        assert core.target_state(r, mid) == "live"
        assert core.stats(r, IID)["sent"] == "1"

    def test_concurrent_polls_never_share_a_message(self, r):
        ids = [_create(r) for _ in range(5)]
        a = core.claim(r, IID, limit=3, now=T0 + 1)
        b = core.claim(r, IID, limit=3, now=T0 + 1)
        assert {m["id"] for m in a}.isdisjoint({m["id"] for m in b})
        assert sorted(m["id"] for m in a + b) == ids

    def test_oldest_first(self, r):
        ids = [_create(r) for _ in range(3)]
        assert [m["id"] for m in core.claim(r, IID, now=T0 + 1)] == ids

    def test_other_integrations_queue_is_separate(self, r):
        _create(r, iid=8)
        assert core.claim(r, IID, now=T0 + 1) == []

    def test_expired_lease_is_handed_out_again(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        again = core.claim(r, IID, now=T0 + 2 + core.LEASE_SECONDS)
        assert [m["id"] for m in again] == [mid]
        assert again[0]["attempt"] == 2

    def test_retryable_failure_requeues_until_attempts_run_out(self, r):
        mid = _create(r)
        for attempt in range(1, core.MAX_ATTEMPTS):
            core.claim(r, IID, now=T0 + attempt)
            assert core.ack(r, IID, mid, ok=False, http_status=503,
                            now=T0 + attempt)["status"] == "retrying"
        core.claim(r, IID, now=T0 + 100)
        assert core.ack(r, IID, mid, ok=False, http_status=429, now=T0 + 100)["status"] == "failed"
        assert core.target_state(r, mid) == "dead"

    def test_permanent_failure_is_not_retried(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        out = core.ack(r, IID, mid, ok=False, http_status=403,
                       code=core.MISSING_PERMISSIONS, error="Missing Permissions", now=T0 + 2)
        assert out["status"] == "failed"
        assert core.claim(r, IID, now=T0 + 3) == []
        log = core.recent_log(r, IID)
        assert log[0]["status"] == "failed" and "Missing Permissions" in log[0]["error"]

    def test_create_ack_without_message_id_fails(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        assert core.ack(r, IID, mid, ok=True, now=T0 + 2)["status"] == "failed"

    def test_ack_from_another_integration_is_refused(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        with pytest.raises(LookupError):
            core.ack(r, 99, mid, ok=True, discord_message_id="1", now=T0 + 2)

    def test_ack_of_unclaimed_message_is_refused(self, r):
        mid = _create(r)
        with pytest.raises(ValueError):
            core.ack(r, IID, mid, ok=True, discord_message_id="1", now=T0 + 1)

    def test_duplicate_ack_is_harmless(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        core.ack(r, IID, mid, ok=True, discord_message_id="5", now=T0 + 2)
        again = core.ack(r, IID, mid, ok=True, discord_message_id="5", now=T0 + 3)
        assert again == {"status": "sent", "duplicate": True}

    def test_stale_create_expires_instead_of_posting_late(self, r):
        mid = _create(r, now=T0)
        assert core.claim(r, IID, hold_minutes=60, now=T0 + 61 * 60) == []
        assert core.load(r, mid)["status"] == "expired"
        assert core.stats(r, IID)["expired"] == "1"


class TestEditsAndDeletes:
    def test_edit_of_unsent_create_replaces_its_body(self, r):
        mid = _create(r, {"content": "v1"})
        assert core.enqueue_edit(r, IID, mid, {"content": "v2"}, now=T0 + 1) == mid
        batch = core.claim(r, IID, now=T0 + 2)
        assert len(batch) == 1 and batch[0]["body"] == {"content": "v2"}

    def test_edit_waits_behind_its_unacked_create(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)  # create leased, not acked
        edit = core.enqueue_edit(r, IID, mid, {"content": "v2"}, now=T0 + 2)
        assert edit != mid
        assert core.claim(r, IID, now=T0 + 3) == []  # ordering kept
        core.ack(r, IID, mid, ok=True, discord_message_id="777", now=T0 + 4)
        batch = core.claim(r, IID, now=T0 + 5)
        assert batch[0]["op"] == "edit" and batch[0]["message_id"] == "777"
        assert batch[0]["channel_id"] == "111"

    def test_unsent_edits_coalesce(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        core.ack(r, IID, mid, ok=True, discord_message_id="1", now=T0 + 2)
        e1 = core.enqueue_edit(r, IID, mid, {"content": "a"}, now=T0 + 3)
        e2 = core.enqueue_edit(r, IID, mid, {"content": "b"}, now=T0 + 4)
        assert e1 == e2
        batch = core.claim(r, IID, now=T0 + 5)
        assert [m["body"] for m in batch] == [{"content": "b"}]

    def test_edit_after_a_sent_edit_queues_a_new_one(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        core.ack(r, IID, mid, ok=True, discord_message_id="1", now=T0 + 2)
        e1 = core.enqueue_edit(r, IID, mid, {"content": "a"}, now=T0 + 3)
        core.claim(r, IID, now=T0 + 4)
        core.ack(r, IID, e1, ok=True, now=T0 + 5)
        e2 = core.enqueue_edit(r, IID, mid, {"content": "b"}, now=T0 + 6)
        assert e2 != e1

    def test_unknown_message_on_edit_marks_the_target_gone(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        core.ack(r, IID, mid, ok=True, discord_message_id="1", now=T0 + 2)
        e = core.enqueue_edit(r, IID, mid, {"content": "x"}, now=T0 + 3)
        core.claim(r, IID, now=T0 + 4)
        out = core.ack(r, IID, e, ok=False, http_status=404, code=core.UNKNOWN_MESSAGE,
                       now=T0 + 5)
        assert out == {"status": "failed", "target": "deleted"}
        assert core.target_state(r, mid) == "dead"  # the feature will repost

    def test_edit_of_unknown_target_raises(self, r):
        with pytest.raises(LookupError):
            core.enqueue_edit(r, IID, 12345, {"content": "x"})

    def test_delete_withdraws_an_unsent_create(self, r):
        mid = _create(r)
        assert core.enqueue_delete(r, IID, mid, now=T0 + 1) is None
        assert core.claim(r, IID, now=T0 + 2) == []
        assert core.target_state(r, mid) == "dead"

    def test_delete_of_sent_message_is_delivered(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        core.ack(r, IID, mid, ok=True, discord_message_id="55", now=T0 + 2)
        d = core.enqueue_delete(r, IID, mid, now=T0 + 3)
        batch = core.claim(r, IID, now=T0 + 4)
        assert batch == [{"id": d, "op": "delete", "channel_id": "111",
                          "attempt": 1, "message_id": "55"}]
        core.ack(r, IID, d, ok=True, now=T0 + 5)
        assert core.target_state(r, mid) == "dead"

    def test_edit_of_a_failed_target_fails_instead_of_waiting_forever(self, r):
        mid = _create(r)
        core.claim(r, IID, now=T0 + 1)
        core.ack(r, IID, mid, ok=True, discord_message_id="1", now=T0 + 2)
        e = core.enqueue_edit(r, IID, mid, {"content": "x"}, now=T0 + 3)
        # Someone deletes the target in the meantime (e.g. an earlier edit 404'd).
        rec = core.load(r, mid)
        rec["status"] = "deleted"
        core._save(r, rec, 60)
        assert core.claim(r, IID, now=T0 + 4) == []
        assert core.load(r, e)["status"] == "failed"


class TestFiles:
    def test_files_are_stored_listed_and_owned(self, r):
        mid = _create(r, files=[("board.png", b"\x89PNG..."), ("x.txt", "not bytes")])
        batch = core.claim(r, IID, now=T0 + 1)
        assert batch[0]["files"] == [{"index": 0, "filename": "board.png", "size": 7,
                                      "url": f"/v2/relay/files/{mid}/0"}]
        assert core.get_file(r, IID, mid, 0) == b"\x89PNG..."
        assert core.get_file(r, 99, mid, 0) is None  # another integration

    def test_oversized_file_is_skipped(self, r):
        big = b"x" * (core.MAX_FILE_BYTES + 1)
        _create(r, files=[("big.bin", big)])
        assert core.claim(r, IID, now=T0 + 1)[0]["files"] == []

    def test_files_are_released_after_a_failed_send(self, r):
        mid = _create(r, files=[("a.png", b"123")])
        core.claim(r, IID, now=T0 + 1)
        core.ack(r, IID, mid, ok=False, http_status=403, code=core.MISSING_ACCESS, now=T0 + 2)
        assert core.get_file(r, IID, mid, 0) is None

    def test_coalesced_edit_replaces_files(self, r):
        mid = _create(r, files=[("a.png", b"old")])
        core.enqueue_edit(r, IID, mid, {"content": "v2"}, files=[("a.png", b"new")])
        assert core.get_file(r, IID, mid, 0) == b"new"


# ── payload rewriting ───────────────────────────────────────────────────────

class TestRewriteBody:
    FOREIGN = {"111111111111111111": "🧭", "222222222222222222": ""}

    def test_app_emojis_in_text_fall_back(self):
        body = {"components": [{"type": 17, "components": [
            {"type": 10, "content": "Hi <:compass:111111111111111111> <a:spin:222222222222222222>x"}]}]}
        out = core.rewrite_body(body, self.FOREIGN)
        assert out["components"][0]["components"][0]["content"] == "Hi 🧭 x"

    def test_guild_emojis_are_left_alone(self):
        body = {"content": "<:clan:333333333333333333>"}
        assert core.rewrite_body(body, self.FOREIGN)["content"] == "<:clan:333333333333333333>"

    def test_button_emoji_falls_back_or_is_dropped(self):
        body = {"components": [{"type": 1, "components": [
            {"type": 2, "style": 1, "label": "A", "custom_id": "evtsignup:5",
             "emoji": {"id": "111111111111111111", "name": "compass"}},
            {"type": 2, "style": 1, "label": "B", "custom_id": "evtsignup:6",
             "emoji": {"id": "222222222222222222", "name": "spin"}},
        ]}]}
        buttons = core.rewrite_body(body, self.FOREIGN)["components"][0]["components"]
        assert buttons[0]["emoji"] == {"name": "🧭"}
        assert "emoji" not in buttons[1]
        assert buttons[1]["custom_id"] == "evtsignup:6"

    def test_activity_launch_becomes_a_link(self):
        body = {"components": [{"type": 1, "components": [
            {"type": 2, "style": 1, "label": "Open", "custom_id": "activity_launch_open:e:42:review"},
            {"type": 2, "style": 1, "label": "App", "custom_id": "activity_launch_open"},
        ]}]}
        b1, b2 = core.rewrite_body(body)["components"][0]["components"]
        assert b1 == {"type": 2, "style": 5, "label": "Open",
                      "url": "https://www.droptracker.io/events/42"}
        assert b2["url"] == "https://www.droptracker.io" and "custom_id" not in b2

    def test_nonce_and_default_tts_are_stripped(self):
        out = core.rewrite_body({"content": "x", "nonce": "abc", "enforce_nonce": True,
                                 "tts": False})
        assert out == {"content": "x"}
        assert core.rewrite_body({"content": "x", "tts": True})["tts"] is True

    def test_custom_ids_and_urls_are_never_rewritten(self):
        body = {"components": [{"type": 2, "style": 5, "label": "x",
                                "url": "https://x/<:a:111111111111111111>"}]}
        assert core.rewrite_body(body, self.FOREIGN) == body


# ── interactions ────────────────────────────────────────────────────────────

def _click(custom_id="evtsignup:5", app="900", guild="800", user="700", **extra):
    payload = {"id": "1", "token": "tok", "application_id": app, "type": 3,
               "guild_id": guild, "channel_id": "600",
               "member": {"user": {"id": user}, "roles": [], "permissions": "0"},
               "data": {"custom_id": custom_id, "component_type": 2}}
    payload.update(extra)
    return payload


class TestInteractions:
    @pytest.mark.parametrize("cid,mode", [
        ("evtsignup:5", "ephemeral"),
        ("evtsignup_acct:5", "update"),
        ("evtsignup_team:5:9", "update"),
        ("dtrv:e:a:12", "update"),
        ("dtrv:m:u:3:4", "update"),
    ])
    def test_defer_modes(self, cid, mode):
        assert core.validate_interaction(_click(cid), "900", ["800"]) == (mode, None)

    @pytest.mark.parametrize("payload,app,guilds,needle", [
        (_click(app="901"), "900", ["800"], "different application"),
        (_click(guild="801"), "900", ["800"], "server"),
        (_click("some_other_bot:1"), "900", ["800"], "not a DropTracker"),
        (_click(type=2), "900", ["800"], "component"),
        (_click(), None, ["800"], "no application id"),
        (_click(token=""), "900", ["800"], "token"),
        ("nope", "900", ["800"], "raw interaction"),
    ])
    def test_rejections(self, payload, app, guilds, needle):
        mode, problem = core.validate_interaction(payload, app, guilds)
        assert mode is None and needle in problem

    def test_rules_cover_the_real_handlers_prefixes(self):
        """The relay must forward exactly the custom_ids our handlers own."""
        signup = (_ROOT / "services" / "event_signup_discord.py").read_text()
        prefixes = re.findall(r'^_(?:PROMPT|ACCT|TEAM) = "([^"]+)"', signup, re.M)
        review = (_ROOT / "services" / "review_buttons_core.py").read_text()
        prefixes += re.findall(r'^PREFIX = "([^"]+)"', review, re.M)
        assert len(prefixes) == 4
        for p in prefixes:
            assert core.interaction_defer(p + "1") is not None, p

    def test_launch_constants_match_the_activity_module(self):
        launch = _load("_launch_core_for_relay", "services", "activity_launch_core.py")
        assert core.LAUNCH_CUSTOM_ID == launch.LAUNCH_BUTTON_CUSTOM_ID
        assert core.LAUNCH_EVENT_INFIX == launch.LAUNCH_EVENT_INFIX

    def test_push_interaction_queues_for_the_core_bot(self, r):
        core.push_interaction(r, IID, 299, _click(), "ephemeral", now=T0)
        item = r.brpop(core.INTERACTION_QUEUE)
        assert b'"group_id":299' in item[1] and b'"defer":"ephemeral"' in item[1]


class TestFeatures:
    def test_feature_mapping(self):
        assert core.feature_for_notification_type("event_completion") == "events"
        assert core.feature_for_notification_type("drop") == "notifications"
        assert core.feature_for_notification_type(None) == "notifications"

    def test_normalize(self):
        assert core.normalize_features(["events", "bogus", "events"]) == ["events"]
        assert core.normalize_features('["notifications"]') == ["notifications"]
        assert core.normalize_features(None) == []

    def test_web_api_mirror_matches(self):
        import ast

        tree = ast.parse((_ROOT / "web_api" / "routes" / "discord_relay_admin.py").read_text())
        consts = {t.id: ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) for t in n.targets
                  if isinstance(t, ast.Name) and t.id in ("FEATURES", "STATUSES",
                                                          "ROUTES_VERSION_KEY")}
        assert consts == {"FEATURES": core.FEATURES, "STATUSES": core.STATUSES,
                          "ROUTES_VERSION_KEY": core.ROUTES_VERSION_KEY}


class TestChannelMap:
    def test_remember_and_resolve(self, r):
        assert core.remember_channels(r, "800", ["1", "2", "x"]) == 2
        assert core.channel_guild(r, "1") == "800"
        assert core.channel_guild(r, "3") is None


# ── the core bot's stand-ins (real interactions components) ─────────────────

@pytest.fixture(scope="module")
def real_interactions():
    """The REAL interactions package for this module's stand-in tests.

    The conftest stubs ``interactions``; the whole point here is that the
    queued body is what the library itself would send (V2 flag, mention and
    attachment shapes), so swap the real package in and put the stub back
    afterwards so no other test sees a change."""
    saved = {k: v for k, v in sys.modules.items()
             if k == "interactions" or k.startswith("interactions.")}
    for k in saved:
        del sys.modules[k]
    try:
        import interactions  # noqa: F401  (the real one)

        yield interactions
    finally:
        for k in [k for k in sys.modules
                  if k == "interactions" or k.startswith("interactions.")]:
            del sys.modules[k]
        sys.modules.update(saved)


@pytest.fixture
def relay(monkeypatch, r, real_interactions):
    """services.discord_relay with the real core and a fake Redis."""
    pkg = sys.modules.get("services")
    monkeypatch.setitem(sys.modules, "services.discord_relay_core", core)
    if pkg is not None:
        monkeypatch.setattr(pkg, "discord_relay_core", core, raising=False)
    mod = _load("_relay_bot_under_test", "services", "discord_relay.py")
    monkeypatch.setattr(mod, "_redis", lambda: r)
    monkeypatch.setattr(mod, "_foreign_emojis", {"111111111111111111": "🧭"})
    return mod


def _route(mod):
    return mod.Route(integration_id=IID, group_id=299, guild_id="800",
                     features=("events",), hold_minutes=360, application_id="900")


class TestRelayChannel:
    def test_send_queues_the_libraries_own_v2_body(self, relay, r):
        import interactions
        from interactions import ContainerComponent, TextDisplayComponent

        ch = relay.RelayChannel(_route(relay), 111, ref="event_board:5")
        msg = asyncio.run(ch.send(
            components=[ContainerComponent(TextDisplayComponent("Board <:c:111111111111111111>"))],
            files=interactions.File(__import__("io").BytesIO(b"PNG"), file_name="board.png"),
            allowed_mentions=interactions.AllowedMentions(parse=["roles"])))
        rec = core.load(r, msg.mid)
        body = rec["body"]
        assert body["flags"] & 1 << 15  # IS_COMPONENTS_V2, set by the library
        assert body["components"][0]["components"][0]["content"] == "Board 🧭"
        assert body["attachments"] == [{"id": 0, "filename": "board.png"}]
        assert body["allowed_mentions"]["parse"] == ["roles"]
        assert rec["files"][0]["filename"] == "board.png"
        assert core.get_file(r, IID, msg.mid, 0) == b"PNG"
        assert msg.id == core.synthetic_id(msg.mid)
        assert rec["ref"] == "event_board:5"

    def test_fetch_message_round_trip_and_edit(self, relay, r):
        ch = relay.RelayChannel(_route(relay), 111)
        msg = asyncio.run(ch.send(content="v1"))
        again = asyncio.run(ch.fetch_message(message_id=str(msg.id)))
        assert again is not None and again.mid == msg.mid
        asyncio.run(again.edit(content="v2", attachments=[]))
        # Still unsent → the create itself now carries v2.
        assert core.load(r, msg.mid)["body"]["content"] == "v2"

    def test_fetch_message_mirrors_interactions_for_missing(self, relay, r):
        ch = relay.RelayChannel(_route(relay), 111)
        # A real snowflake (posted by our bot before the relay) or a gone
        # relay message reads as None — callers repost on None.
        assert asyncio.run(ch.fetch_message(message_id="1413573204886294599")) is None
        msg = asyncio.run(ch.send(content="x"))
        asyncio.run(msg.delete())
        assert asyncio.run(ch.fetch_message(message_id=str(msg.id))) is None

    def test_edit_that_removes_buttons_says_so(self, relay, r):
        ch = relay.RelayChannel(_route(relay), 111)
        msg = asyncio.run(ch.send(content="x"))
        core.claim(r, IID)
        core.ack(r, IID, msg.mid, ok=True, discord_message_id="5")
        asyncio.run(msg.edit(components=[]))
        batch = core.claim(r, IID)
        assert batch[0]["body"]["components"] == []

    def test_is_never_treated_as_a_thread(self, relay):
        ch = relay.RelayChannel(_route(relay), 111)
        assert ch.type == 0 and relay.is_relayed(ch)


class _Bot:
    def __init__(self, guild_id=None):
        self.calls = 0
        self.guild_id = guild_id

    async def fetch_channel(self, channel_id=None):
        self.calls += 1
        return types.SimpleNamespace(id=int(channel_id), _guild_id=self.guild_id,
                                     send=lambda *a, **k: None)


class TestFetchChannel:
    def test_no_integrations_is_plain_fetch(self, relay, monkeypatch):
        monkeypatch.setattr(relay, "routes", lambda force=False: {})
        bot = _Bot(800)
        ch = asyncio.run(relay.fetch_channel(bot, 5))
        assert not relay.is_relayed(ch) and bot.calls == 1

    def test_relay_owned_guild_gets_a_relay_channel(self, relay, monkeypatch, r):
        route = _route(relay)
        monkeypatch.setattr(relay, "routes", lambda force=False: {"800": route})
        bot = _Bot(800)
        ch = asyncio.run(relay.fetch_channel(bot, 5, feature="events"))
        assert relay.is_relayed(ch) and ch.route is route
        # Learned into the channel map, so the next lookup skips our bot.
        assert core.channel_guild(r, "5") == "800"
        ch2 = asyncio.run(relay.fetch_channel(bot, 5, feature="events"))
        assert relay.is_relayed(ch2) and bot.calls == 1

    def test_feature_not_taken_over_stays_with_our_bot(self, relay, monkeypatch):
        monkeypatch.setattr(relay, "routes", lambda force=False: {"800": _route(relay)})
        ch = asyncio.run(relay.fetch_channel(_Bot(800), 5, feature="notifications"))
        assert not relay.is_relayed(ch)

    def test_channel_map_works_without_our_bot_in_the_server(self, relay, monkeypatch, r):
        monkeypatch.setattr(relay, "routes", lambda force=False: {"800": _route(relay)})
        core.remember_channels(r, "800", ["5"])
        bot = _Bot(None)
        ch = asyncio.run(relay.fetch_channel(bot, 5))
        assert relay.is_relayed(ch) and bot.calls == 0


class _Responder:
    def __init__(self):
        self.calls = []

    async def initial(self, payload):
        self.calls.append(("initial", payload))

    async def edit_original(self, payload):
        self.calls.append(("edit_original", payload))

    async def followup(self, payload):
        self.calls.append(("followup", payload))


class TestRelayComponentContext:
    def test_ephemeral_defer_fills_the_thinking_reply_first(self, relay):
        resp = _Responder()
        ctx = relay.RelayComponentContext(_click("evtsignup:5"), "ephemeral", resp)
        assert ctx.user.id == 700 and not ctx.editing_origin
        asyncio.run(ctx.defer(ephemeral=True))
        asyncio.run(ctx.send("first", ephemeral=True))
        asyncio.run(ctx.send("second", ephemeral=True))
        assert [c[0] for c in resp.calls] == ["edit_original", "followup"]
        assert resp.calls[0][1] == {"content": "first"}
        assert resp.calls[1][1] == {"content": "second", "flags": 64}

    def test_update_defer_edits_the_origin(self, relay):
        resp = _Responder()
        payload = _click("evtsignup_acct:5")
        payload["data"]["values"] = ["123"]
        ctx = relay.RelayComponentContext(payload, "update", resp)
        assert ctx.editing_origin and ctx.values == ["123"]
        asyncio.run(ctx.edit_origin(content="done", components=[], embeds=[]))
        assert resp.calls == [("edit_original", {"content": "done", "components": [],
                                                 "embeds": []})]


class TestResponderRetry:
    def test_404_is_retried_then_succeeds(self, relay, monkeypatch):
        rsp = relay.InteractionResponder("900", "tok")
        calls = []

        async def fake_request(method, path, payload):
            calls.append(path)
            if len(calls) < 3:
                raise relay.DiscordHTTPError(404, "Unknown Webhook")
            return {"id": "1"}

        async def no_sleep(_):
            return None

        monkeypatch.setattr(rsp, "_request", fake_request)
        monkeypatch.setattr(relay.asyncio, "sleep", no_sleep)
        assert asyncio.run(rsp.edit_original({"content": "x"})) == {"id": "1"}
        assert len(calls) == 3 and calls[0].endswith("/messages/@original")

    def test_other_errors_are_not_retried(self, relay, monkeypatch):
        rsp = relay.InteractionResponder("900", "tok")
        calls = []

        async def fake_request(method, path, payload):
            calls.append(path)
            raise relay.DiscordHTTPError(400, "bad")

        monkeypatch.setattr(rsp, "_request", fake_request)
        with pytest.raises(relay.DiscordHTTPError):
            asyncio.run(rsp.followup({"content": "x"}))
        assert len(calls) == 1
