"""services/clan_chat_muted.py: telling a muted clanmate's lines apart and
delivering them to the clan's plugin users.

Loaded from the file path because the conftest stubs the ``services``
package; Redis, presence and inbox pushes are replaced with in-memory fakes.
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


muted = _load("_clan_chat_muted_under_test", "services", "clan_chat_muted.py")
bridge = _load("_clan_chat_bridge_for_muted", "services", "clan_chat_bridge.py")

SLUG = "the best clan"
DIGEST = "abc123"
SEEN = muted.SEEN_KEY_TEMPLATE.format(clan_slug=SLUG, digest=DIGEST)


class _FakeRedis:
    def __init__(self):
        self.kv = {}
        self.hashes = {}
        self.sets = {}
        self.zsets = {}

    # strings
    def exists(self, key):
        return 1 if key in self.kv else 0

    def set(self, key, value, ex=None):
        self.kv[key] = value

    def delete(self, *keys):
        for key in keys:
            self.kv.pop(key, None)

    # hashes / sets
    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hincrby(self, key, field, amount=1):
        h = self.hashes.setdefault(key, {})
        h[field] = int(h.get(field, 0)) + amount

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    def expire(self, *_a):
        return True

    # sorted sets
    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zrangebyscore(self, key, lo, hi, start=0, num=None):
        z = self.zsets.get(key, {})
        hi = float("inf") if hi == "+inf" else float(hi)
        items = sorted((s, m) for m, s in z.items() if s <= hi)
        return [m for _s, m in items][: num or None]

    def zrem(self, key, *members):
        for m in members:
            self.zsets.get(key, {}).pop(m, None)

    def pipeline(self):
        return _Pipe(self)


class _Pipe:
    def __init__(self, r):
        self.r = r
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append((name, a, k))
            return self
        return call

    def execute(self):
        return [getattr(self.r, n)(*a, **k) for n, a, k in self.calls]


def _env(monkeypatch, online):
    fake = _FakeRedis()
    pushed = []
    monkeypatch.setattr(muted, "_redis", lambda: fake)
    monkeypatch.setattr(muted, "_online", lambda _slug: list(online))
    monkeypatch.setattr(muted, "_push", lambda pid, payload: pushed.append((pid, payload)) or True)
    return fake, pushed


def _say(sender_id=1, now=None):
    now = muted.time.time() if now is None else now
    return muted.on_self_line(SLUG, DIGEST, sender_id, "Muted Mike", "can anyone hear me",
                              account_type="ironman", now=now)


def test_unheard_line_is_delivered_after_the_window_and_flags_the_sender(monkeypatch):
    fake, pushed = _env(monkeypatch, online=[1, 2, 3])
    fake.hashes[SEEN] = {"1": 1, "_shown": 1}  # only the sender reported it
    assert _say() == 0  # first line waits for the window
    assert pushed == []

    # Nothing is due before the window closes.
    t0 = json.loads(next(iter(fake.zsets[muted.CHECK_QUEUE_KEY])))["ts"]
    assert muted.process_due_checks(now=t0 + muted.CHECK_WINDOW_SECONDS - 1)["unheard"] == 0
    counts = muted.process_due_checks(now=t0 + muted.CHECK_WINDOW_SECONDS + 1)
    assert counts["unheard"] == 1
    assert sorted(pid for pid, _p in pushed) == [2, 3]
    assert pushed[0][1] == {"sender": "Muted Mike", "message": "can anyone hear me",
                            "account_type": "ironman"}
    assert muted.is_flagged(SLUG, 1)


def test_heard_line_is_not_delivered_and_clears_the_flag(monkeypatch):
    fake, pushed = _env(monkeypatch, online=[1, 2])
    fake.kv[muted.MUTED_FLAG_KEY_TEMPLATE.format(clan_slug=SLUG, player_id=1)] = "1"
    fake.hashes[SEEN] = {"1": 1, "2": 1, "_shown": 1}  # clanmate 2 saw it too
    _say()
    pushed.clear()  # the flagged fast path delivered already; the check decides
    assert muted.process_due_checks(now=muted.time.time() + 60)["heard"] == 1
    assert pushed == []
    assert not muted.is_flagged(SLUG, 1)


def test_flagged_sender_is_delivered_at_once(monkeypatch):
    fake, pushed = _env(monkeypatch, online=[1, 2, 3])
    fake.kv[muted.MUTED_FLAG_KEY_TEMPLATE.format(clan_slug=SLUG, player_id=1)] = "1"
    fake.hashes[SEEN] = {"1": 1, "_shown": 1}
    assert _say() == 2
    # The check later finds it still unheard and sends nothing twice.
    muted.process_due_checks(now=muted.time.time() + 60)
    assert sorted(pid for pid, _p in pushed) == [2, 3]
    assert muted.is_flagged(SLUG, 1)


def test_a_stale_backlog_flags_but_does_not_deliver(monkeypatch):
    fake, pushed = _env(monkeypatch, online=[1, 2])
    fake.hashes[SEEN] = {"1": 1, "_shown": 1}
    _say(now=muted.time.time() - 600)
    assert muted.process_due_checks()["unheard"] == 1
    assert pushed == []
    assert muted.is_flagged(SLUG, 1)


def test_nobody_else_online_proves_nothing(monkeypatch):
    fake, pushed = _env(monkeypatch, online=[1])
    assert _say() == 0
    assert fake.zsets.get(muted.CHECK_QUEUE_KEY, {}) == {}
    assert not muted.is_flagged(SLUG, 1)


def test_queue_records_are_json(monkeypatch):
    fake, _pushed = _env(monkeypatch, online=[1, 2])
    _say()
    (raw,) = fake.zsets[muted.CHECK_QUEUE_KEY]
    assert json.loads(raw)["sender_id"] == 1


def test_discord_mirror_marks_a_muted_sender():
    entry = {"channel_id": "9", "kind": "chat", "sender": "Muted Mike",
             "message": "hello", "muted": "1"}
    (line,) = bridge.batch_lines_by_channel([entry], rank_emojis={})["9"]
    assert line == f"{bridge.MUTED_ICON} **Muted Mike**: hello"
