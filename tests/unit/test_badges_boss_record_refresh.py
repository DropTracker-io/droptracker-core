"""Bug #250: a new boss record moves its badge when the PB lands, not at the
next hourly cycle."""
import importlib.util
import os
import sys
from types import SimpleNamespace

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "badges.py",
)
_spec = importlib.util.spec_from_file_location("_badges_refresh_under_test", _PATH)
badges = importlib.util.module_from_spec(_spec)
sys.modules["_badges_refresh_under_test"] = badges
_spec.loader.exec_module(badges)


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Query:
    def __init__(self, value):
        self._value = value

    def filter(self, *a, **k):
        return self

    def scalar(self):
        return self._value


class _Session:
    def __init__(self, fastest):
        self.fastest = fastest
        self.committed = self.closed = False

    def execute(self, *_a, **_k):
        return _Result(self.fastest)

    def query(self, *_a):
        return _Query("Hespori")

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def _wire(monkeypatch, fastest):
    session = _Session(fastest)
    calls = []
    badge = SimpleNamespace(badge_id=7, key="boss_record")
    monkeypatch.setattr(badges, "Session", lambda: session)
    monkeypatch.setattr(badges, "_load_automatic_badges",
                        lambda _s: [(badge, {"type": "boss_record"})])

    def fake_transfer(_s, b, slot_key, player_id, context):
        calls.append((slot_key, player_id, context["pb_ms"], context["npc_name"]))
        return "transferred"

    monkeypatch.setattr(badges, "transfer_held_badge", fake_transfer)
    return session, calls


def test_refresh_moves_the_slot_to_the_fastest(monkeypatch):
    session, calls = _wire(monkeypatch, (5751994, 18600))
    assert badges.refresh_boss_record(8583, "Solo") == "transferred"
    assert calls == [("npc:8583:Solo", 5751994, 18600, "Hespori")]
    assert session.committed and session.closed


def test_unclean_team_size_is_ignored(monkeypatch):
    _session, calls = _wire(monkeypatch, (1, 1))
    assert badges.refresh_boss_record(8583, "5 s") is None
    assert calls == []


def test_no_pb_rows_skips(monkeypatch):
    _session, calls = _wire(monkeypatch, None)
    assert badges.refresh_boss_record(8583, "Solo") == "skipped"
    assert calls == []
