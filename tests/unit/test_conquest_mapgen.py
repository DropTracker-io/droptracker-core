"""services/conquest_mapgen.py: the selection cache and the one-at-a-time
lock (the generator itself is scripts/conquest_map, run as a subprocess)."""

import asyncio
import importlib.util
import json
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location(
    "_conquest_mapgen_ut", os.path.join(_ROOT, "services", "conquest_mapgen.py"))
mg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mg)


class FakeRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(mg, "HOME", str(tmp_path))
    fake = FakeRedis()
    monkeypatch.setattr(mg, "_redis", lambda: fake)
    monkeypatch.setattr(mg, "_running", {})
    return tmp_path, fake


def test_key_ignores_order_and_duplicates():
    a = mg.selection_key(["seas", "kourend"], ["yama"])
    assert a == mg.selection_key(["kourend", "seas", "seas"], ["yama"])
    assert a != mg.selection_key(["kourend", "seas"], [])


def test_cached_art_is_ready(home):
    tmp, _fake = home
    key = mg.selection_key(["kourend"], [])
    (tmp / "cache").mkdir()
    (tmp / "cache" / f"{key}.json").write_text(json.dumps({"key": key, "tiles": {}}))
    status, art = asyncio.run(mg.ensure_art(["kourend"], []))
    assert status == "ready" and art["key"] == key
    assert mg.status(key) == "ready"


def test_no_toolchain_is_unavailable(home):
    status, art = asyncio.run(mg.ensure_art(["kourend"], []))
    assert (status, art) == ("unavailable", None)


def test_busy_generator_queues_other_picks(home, monkeypatch):
    _tmp, fake = home
    monkeypatch.setattr(mg, "toolchain_ready", lambda: True)
    fake.data[mg.LOCK_KEY] = "someone-else"
    assert asyncio.run(mg.ensure_art(["kourend"], [])) == ("queued", None)
    key = mg.selection_key(["kourend"], [])
    fake.data[mg.LOCK_KEY] = key
    assert asyncio.run(mg.ensure_art(["kourend"], [])) == ("generating", None)
