"""utils/split_policy.py — payload parsing, the three-mode gate, and the
shadow counters, without touching the DB."""

import importlib

import pytest

sp = importlib.import_module("utils.split_policy")


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self.results = []

    def __getattr__(self, name):
        def _op(*args, **kwargs):
            self.results.append(getattr(self.store, name)(*args, **kwargs))
            return self

        return _op

    def execute(self):
        out, self.results = self.results, []
        return out


class FakeRedis:
    def __init__(self):
        self.hashes = {}

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def hincrby(self, key, field, amount=1):
        h = self.hashes.setdefault(key, {})
        h[field] = int(h.get(field, 0)) + amount
        return h[field]

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hgetall(self, key):
        return {str(k).encode(): str(v).encode()
                for k, v in self.hashes.get(key, {}).items()}

    def expire(self, key, ttl):
        return True

    def delete(self, *keys):
        for k in keys:
            self.hashes.pop(k, None)
        return len(keys)


@pytest.fixture(autouse=True)
def _clean_caches():
    sp.invalidate()
    yield
    sp.invalidate()


@pytest.fixture
def gate(monkeypatch):
    """Pin the allowlist + mode without any DB access."""
    def _apply(eligible, mode):
        monkeypatch.setattr(sp, "get_eligible", lambda session=None: dict(eligible))
        monkeypatch.setattr(sp, "get_mode", lambda session=None: mode)
    return _apply


# --------------------------------------------------------------------------- #
# Payload parsing
# --------------------------------------------------------------------------- #
def test_parse_object_list_and_csv():
    assert sp._parse('{"13699": "raid", "11278": "team_boss"}') == {
        13699: "raid", 11278: "team_boss"
    }
    assert sp._parse("[13699, 11278]") == {
        13699: sp.CATEGORY_TEAM_BOSS, 11278: sp.CATEGORY_TEAM_BOSS
    }
    assert sp._parse("13699, 11278") == {
        13699: sp.CATEGORY_TEAM_BOSS, 11278: sp.CATEGORY_TEAM_BOSS
    }


def test_parse_empty_and_junk():
    for raw in (None, "", "   ", "[]", "{}"):
        assert sp._parse(raw) == {}
    # Unparseable members are skipped, not fatal.
    assert sp._parse('{"13699": "raid", "abc": "raid"}') == {13699: "raid"}


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_shadow_mode_never_blocks(gate):
    gate({13699: "raid"}, sp.MODE_SHADOW)
    assert sp.allows_split(13699) is True
    assert sp.allows_split(999999) is True  # not eligible, still permitted


def test_off_mode_never_blocks(gate):
    gate({13699: "raid"}, sp.MODE_OFF)
    assert sp.allows_split(999999) is True


def test_enforce_mode_blocks_only_unlisted(gate):
    gate({13699: "raid", 11278: "team_boss"}, sp.MODE_ENFORCE)
    assert sp.allows_split(13699) is True
    assert sp.allows_split(11278) is True
    assert sp.allows_split(999999) is False


def test_enforce_with_empty_allowlist_permits_everything(gate):
    """An unseeded (or unreadable) list must never silently kill splits."""
    gate({}, sp.MODE_ENFORCE)
    assert sp.allows_split(13699) is True
    assert sp.allows_split(999999) is True


def test_unknown_npc_is_permitted(gate):
    gate({13699: "raid"}, sp.MODE_ENFORCE)
    assert sp.allows_split(None) is True
    assert sp.allows_split("not-an-id") is True


def test_is_eligible_is_mode_independent(gate):
    gate({13699: "raid"}, sp.MODE_SHADOW)
    assert sp.is_eligible(13699) is True
    assert sp.is_eligible(999999) is False
    assert sp.category_for(13699) == "raid"
    assert sp.category_for(999999) is None


# --------------------------------------------------------------------------- #
# Shadow telemetry
# --------------------------------------------------------------------------- #
def test_record_split_event_counts_both_sides(gate):
    gate({13699: "raid"}, sp.MODE_SHADOW)
    r = FakeRedis()
    sp.record_split_event(13699, "Theatre of Blood", r=r)
    sp.record_split_event(999999, "Knight", r=r)
    sp.record_split_event(999999, "Knight", r=r)

    snap = sp.impact_snapshot(r=r)
    assert snap["allowed"] == {13699: 1}
    assert snap["blocked"] == {999999: {"name": "Knight", "count": 2}}


def test_record_split_event_silent_when_unconfigured(gate):
    """No allowlist seeded => the counters mean nothing, so record nothing."""
    gate({}, sp.MODE_SHADOW)
    r = FakeRedis()
    sp.record_split_event(13699, "Theatre of Blood", r=r)
    assert sp.impact_snapshot(r=r) == {"blocked": {}, "allowed": {}}


def test_record_split_event_silent_when_mode_off(gate):
    gate({13699: "raid"}, sp.MODE_OFF)
    r = FakeRedis()
    sp.record_split_event(999999, "Knight", r=r)
    assert sp.impact_snapshot(r=r) == {"blocked": {}, "allowed": {}}


def test_clear_impact(gate):
    gate({13699: "raid"}, sp.MODE_SHADOW)
    r = FakeRedis()
    sp.record_split_event(999999, "Knight", r=r)
    sp.clear_impact(r=r)
    assert sp.impact_snapshot(r=r) == {"blocked": {}, "allowed": {}}


def test_telemetry_failures_never_raise(gate):
    gate({13699: "raid"}, sp.MODE_SHADOW)
    dead = object()  # no .pipeline, no .client -> conn is None
    sp.record_split_event(999999, "Knight", r=dead)
    assert sp.impact_snapshot(r=dead) == {"blocked": {}, "allowed": {}}


def test_set_mode_rejects_unknown():
    with pytest.raises(ValueError):
        sp.set_mode(None, "enabled")
