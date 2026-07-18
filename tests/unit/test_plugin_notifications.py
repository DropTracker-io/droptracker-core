"""Plugin notification inbox (services/plugin_notifications.py).

Pure-logic tests: envelope shape, inbox push/drain semantics against a fake
Redis, notice capping, fan-out early-outs, and consistency between the plugin
audience map and the notification_queue type registry. The session-dependent
audience/pref queries are exercised against the real DB in integration.

Loaded directly from the file path (like test_event_notifications.py) because
the conftest stubs the ``services`` package; db/redis imports are lazy inside
the functions under test, so the pure paths never touch them.
"""

import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


pn = _load("_plugin_notifications_under_test", "services", "plugin_notifications.py")
en = _load("_event_notifications_for_pn_test", "services", "event_notifications.py")


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def rpush(self, key, value):
        self.ops.append(("rpush", key, value))

    def ltrim(self, key, start, end):
        self.ops.append(("ltrim", key, start, end))

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))

    def lrange(self, key, start, end):
        self.ops.append(("lrange", key, start, end))

    def publish(self, channel, message):
        self.ops.append(("publish", channel, message))

    def execute(self):
        results = []
        for op in self.ops:
            name, key = op[0], op[1]
            lst = self.store.setdefault(key, [])
            if name == "rpush":
                lst.append(op[2])
                results.append(len(lst))
            elif name == "ltrim":
                self.store[key] = _slice(lst, op[2], op[3])
                results.append(True)
            elif name == "expire":
                self.store.setdefault("_ttls", {})[key] = op[2]
                results.append(True)
            elif name == "lrange":
                results.append(list(_slice(lst, op[2], op[3])))
            elif name == "publish":
                self.store.setdefault("_published", []).append((op[1], op[2]))
                results.append(1)
        self.ops = []
        return results


def _slice(lst, start, end):
    n = len(lst)
    if start < 0:
        start = max(n + start, 0)
    if end < 0:
        end = n + end
    return lst[start:end + 1]


class FakeRedis:
    def __init__(self):
        self.store = {}

    def pipeline(self):
        return FakePipeline(self.store)


def _use_fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(pn, "_redis", lambda: fake)
    return fake


class TestEnvelope:
    def test_shape(self):
        env = pn.build_envelope("event_completion", {"task_label": "x"},
                                event={"id": 7, "name": "Bingo"}, now=1000)
        assert env["type"] == "event_completion"
        assert env["ts"] == 1000
        assert env["id"].startswith("1000-")
        assert env["data"] == {"task_label": "x"}
        assert env["event"] == {"id": 7, "name": "Bingo"}

    def test_no_event_key_without_event(self):
        env = pn.build_envelope("submission_notice", {"message": "hi"})
        assert "event" not in env

    def test_event_without_id_omitted(self):
        env = pn.build_envelope("event_started", {}, event={"name": "x"})
        assert "event" not in env

    def test_data_is_copied(self):
        data = {"a": 1}
        env = pn.build_envelope("event_completion", data)
        env["data"]["a"] = 2
        assert data["a"] == 1


class TestInbox:
    def test_push_and_drain_fifo(self, monkeypatch):
        _use_fake_redis(monkeypatch)
        for i in range(3):
            assert pn.push_to_inbox(42, pn.build_envelope("event_completion", {"n": i}))
        drained = pn.drain_inbox(42)
        assert [e["data"]["n"] for e in drained] == [0, 1, 2]
        assert pn.drain_inbox(42) == []

    def test_push_publishes_longpoll_wake(self, monkeypatch):
        fake = _use_fake_redis(monkeypatch)
        pn.push_to_inbox(42, pn.build_envelope("event_completion", {}))
        assert fake.store["_published"] == [(pn.WAKE_CHANNEL, "42")]

    def test_drain_respects_limit_and_keeps_rest(self, monkeypatch):
        _use_fake_redis(monkeypatch)
        for i in range(5):
            pn.push_to_inbox(42, pn.build_envelope("event_completion", {"n": i}))
        first = pn.drain_inbox(42, limit=2)
        assert [e["data"]["n"] for e in first] == [0, 1]
        rest = pn.drain_inbox(42, limit=10)
        assert [e["data"]["n"] for e in rest] == [2, 3, 4]

    def test_inbox_capped(self, monkeypatch):
        _use_fake_redis(monkeypatch)
        for i in range(pn.INBOX_CAP + 10):
            pn.push_to_inbox(42, pn.build_envelope("event_completion", {"n": i}))
        drained = pn.drain_inbox(42, limit=pn.INBOX_CAP + 10)
        assert len(drained) == pn.INBOX_CAP
        # Oldest entries were evicted, newest kept.
        assert drained[-1]["data"]["n"] == pn.INBOX_CAP + 9

    def test_ttl_set_on_push(self, monkeypatch):
        fake = _use_fake_redis(monkeypatch)
        pn.push_to_inbox(42, pn.build_envelope("event_completion", {}))
        assert fake.store["_ttls"][pn._inbox_key(42)] == pn.INBOX_TTL_SECONDS

    def test_push_without_player_is_noop(self, monkeypatch):
        fake = _use_fake_redis(monkeypatch)
        assert pn.push_to_inbox(None, {"type": "x"}) is False
        assert fake.store == {}

    def test_drain_skips_corrupt_entries(self, monkeypatch):
        fake = _use_fake_redis(monkeypatch)
        pn.push_to_inbox(42, pn.build_envelope("event_completion", {"n": 1}))
        fake.store[pn._inbox_key(42)].insert(0, "not json{")
        drained = pn.drain_inbox(42)
        assert len(drained) == 1
        assert drained[0]["data"]["n"] == 1

    def test_redis_failure_is_swallowed(self, monkeypatch):
        def _boom():
            raise RuntimeError("redis down")
        monkeypatch.setattr(pn, "_redis", _boom)
        assert pn.push_to_inbox(42, {"type": "x"}) is False
        assert pn.drain_inbox(42) == []


class TestSubmissionNotice:
    def test_notice_envelope(self, monkeypatch):
        _use_fake_redis(monkeypatch)
        assert pn.push_submission_notice(42, "Drop processed")
        entry = pn.drain_inbox(42)[0]
        assert entry["type"] == "submission_notice"
        assert entry["data"] == {"message": "Drop processed"}

    def test_notice_capped(self, monkeypatch):
        _use_fake_redis(monkeypatch)
        pn.push_submission_notice(42, "x" * 2000)
        entry = pn.drain_inbox(42)[0]
        assert len(entry["data"]["message"]) == pn.NOTICE_MAX_CHARS

    def test_empty_notice_is_noop(self, monkeypatch):
        fake = _use_fake_redis(monkeypatch)
        assert pn.push_submission_notice(42, "") is False
        assert pn.push_submission_notice(None, "hello") is False
        assert fake.store == {}


class TestFanOutEarlyOuts:
    def test_unknown_type_delivers_nothing(self):
        assert pn.fan_out_event_notification(object(), "event_pot", {"id": 1}, {}) == 0

    def test_team_type_without_team_id(self):
        assert pn.fan_out_event_notification(object(), "event_completion", {"id": 1}, {}) == 0

    def test_event_type_without_event_id(self):
        assert pn.fan_out_event_notification(object(), "event_started", {}, {}) == 0

    def test_fan_out_never_raises(self):
        # A session-shaped object that explodes must not propagate: fan-out
        # sits on the event-apply path.
        class ExplodingSession:
            def query(self, *a, **k):
                raise RuntimeError("db down")
        assert pn.fan_out_event_notification(
            ExplodingSession(), "event_completion", {"id": 1}, {"team_id": 5}) == 0


class TestFocusStamp:
    def test_stamp_and_read(self, monkeypatch):
        store = {}

        class FakeStampRedis:
            def setex(self, key, ttl, value):
                store[key] = (ttl, value)

            def get(self, key):
                entry = store.get(key)
                return entry[1].encode() if entry else None

        monkeypatch.setattr(pn, "_redis", lambda: FakeStampRedis())
        pn.stamp_player_focus(7, 19, 512)
        key = pn.FOCUS_KEY_TEMPLATE.format(player_id=7, event_id=19)
        assert store[key] == (pn.FOCUS_TTL_SECONDS, "512")
        assert pn._stamped_focus_task_id(7, 19) == 512
        assert pn._stamped_focus_task_id(7, 20) is None

    def test_stamp_ignores_missing_args(self, monkeypatch):
        called = []
        monkeypatch.setattr(pn, "_redis", lambda: called.append(1))
        pn.stamp_player_focus(None, 19, 512)
        pn.stamp_player_focus(7, None, 512)
        pn.stamp_player_focus(7, 19, None)
        assert called == []


class TestPickFocusTask:
    TASKS = [
        {"id": 1, "label": "A", "type": "item_target", "target_value": 10},
        {"id": 2, "label": "B", "type": "item_target", "target_value": 4},
        {"id": 3, "label": "C", "type": "pb_target", "target_value": None},
    ]

    def test_stamped_wins_while_incomplete(self):
        task, source = pn.pick_focus_task(
            self.TASKS, {2: {"progress": 3, "completed": False}}, stamped_task_id=1)
        assert (task["id"], source) == (1, "inferred")

    def test_completed_stamp_falls_through(self):
        progress = {1: {"progress": 10, "completed": True},
                    2: {"progress": 3, "completed": False}}
        task, source = pn.pick_focus_task(self.TASKS, progress, stamped_task_id=1)
        assert (task["id"], source) == (2, "team_progress")

    def test_unknown_stamp_falls_through(self):
        task, source = pn.pick_focus_task(
            self.TASKS, {2: {"progress": 1, "completed": False}}, stamped_task_id=999)
        assert (task["id"], source) == (2, "team_progress")

    def test_most_progressed_by_ratio_not_raw(self):
        # 3/4 (75%) beats 5/10 (50%) despite the smaller raw progress.
        progress = {1: {"progress": 5, "completed": False},
                    2: {"progress": 3, "completed": False}}
        task, source = pn.pick_focus_task(self.TASKS, progress)
        assert (task["id"], source) == (2, "team_progress")

    def test_pb_target_needs_one(self):
        task, _ = pn.pick_focus_task(
            self.TASKS, {3: {"progress": 0, "completed": False},
                         1: {"progress": 9, "completed": False}})
        assert task["id"] == 1  # 9/10 beats pb 0/1

    def test_no_progress_gives_first_incomplete(self):
        task, source = pn.pick_focus_task(self.TASKS, {})
        assert (task["id"], source) == (1, "first_task")

    def test_all_completed_gives_none(self):
        progress = {t["id"]: {"progress": 99, "completed": True} for t in self.TASKS}
        task, source = pn.pick_focus_task(self.TASKS, progress)
        assert (task, source) == (None, None)

    def test_ratio_tie_prefers_lower_id(self):
        tasks = [
            {"id": 5, "label": "X", "type": "item_target", "target_value": 10},
            {"id": 4, "label": "Y", "type": "item_target", "target_value": 10},
        ]
        progress = {5: {"progress": 5, "completed": False},
                    4: {"progress": 5, "completed": False}}
        task, _ = pn.pick_focus_task(tasks, progress)
        assert task["id"] == 4


class TestTypeRegistryConsistency:
    def test_audience_types_are_known_queue_types(self):
        for t in pn.AUDIENCE_FOR_TYPE:
            assert t in en.EVENT_NOTIFICATION_TYPES, t

    def test_web_pref_types_are_deliverable(self):
        for t in pn.WEB_PREF_TYPES:
            assert t in pn.AUDIENCE_FOR_TYPE, t

    def test_task_progress_is_client_toggle_only(self):
        # Owner decision 2026-07-17: the noisiest type is muted in game, not
        # on the website — it must never appear in the web pref registry.
        assert "event_task_progress" not in pn.WEB_PREF_TYPES
        assert "event_task_progress" in pn.AUDIENCE_FOR_TYPE

    def test_audiences_valid(self):
        for aud in pn.AUDIENCE_FOR_TYPE.values():
            assert aud in (pn.AUDIENCE_TEAM, pn.AUDIENCE_EVENT)
