"""``POST /player/model`` — what runs inside the request and what runs after.

Investigation of 2026-09-03: ~191k uploads a day, ~74k of them new outfits,
and nearly every slow ``POST /player/model`` journal line was one of the
stored ones. Inside the request a stored upload did a B2 HEAD, PUT, LIST and a
DELETE or two (the prune) before answering. The prune now runs in the same
background task as the gear render; these tests pin that split:

* the store thread decides the protected-fingerprint set but never prunes;
* the follow-up prunes with that set, then renders, each failing alone;
* the route pops the internal carrier key before serialising the response.

The route module is loaded from its file path so the conftest's stubs for
``api.core`` and ``db.models`` stand in for the DB, and the real
``services.player_model`` (conftest-loaded) is monkeypatched per test.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import sys
import threading

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


route = _load("_player_model_route_under_test", "api", "routes", "player_model.py")
player_model = sys.modules["services.player_model"]


class _Query:
    def __init__(self, result):
        self._result = result

    def filter(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result

    def all(self):
        return list(self._result) if isinstance(self._result, list) else []


class _Session:
    """Answers ``query(Player)`` / ``query(PlayerState)`` from a fixed map."""

    def __init__(self, by_model):
        self.by_model = by_model
        self.added = []
        self.commits = 0
        self.closed = False

    def query(self, model):
        return _Query(self.by_model.get(model))

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _State:
    """Stands in for the ORM PlayerState (a MagicMock under the conftest stub,
    whose attributes would otherwise read back as mocks). Class-level
    attributes stand in for the ORM columns the route's filter expressions
    touch (``PlayerState.player_id == ...``)."""

    player_id = None
    model_fingerprint = None
    pinned_model_fingerprint = None

    def __init__(self, player_id=None, model_fingerprint=None,
                 pinned_model_fingerprint=None):
        self.player_id = player_id
        self.model_fingerprint = model_fingerprint
        self.pinned_model_fingerprint = pinned_model_fingerprint


@pytest.fixture(autouse=True)
def _real_state_class(monkeypatch):
    monkeypatch.setattr(route, "PlayerState", _State)


@pytest.fixture
def calls(monkeypatch):
    """Fakes for the storage layer; records every call in order."""
    log = []

    monkeypatch.setattr(player_model, "model_exists",
                        lambda pid, fp, **kw: log.append(("exists", pid, fp)) or False)
    monkeypatch.setattr(player_model, "store_model",
                        lambda pid, fp, data, **kw: log.append(("store", pid, fp, kw.get("pet", False)))
                        or f"https://cdn/{pid}/{fp}.glb")
    monkeypatch.setattr(player_model, "prune_old_models",
                        lambda pid, **kw: log.append(("prune", pid, kw.get("protect"))) or 1)
    return log


class TestStoreThread:
    def test_new_model_is_stored_but_not_pruned_inside_the_request(self, calls, monkeypatch):
        player = _Obj(player_id=42)
        state = _State(model_fingerprint=None, pinned_model_fingerprint="pinned1")
        session = _Session({route.Player: player, route.PlayerState: state})
        monkeypatch.setattr(route, "get_db_session", lambda: session)

        result = route._store("hash", "abcd1234", b"model", None, pin=False)

        assert result["stored"] is True
        assert result["player_id"] == 42
        assert result["url"] == "https://cdn/42/abcd1234.glb"
        # The prune is the follow-up's job now; the request thread only
        # decides what the prune must keep.
        assert result[route._PROTECT_KEY] == frozenset({"pinned1"})
        assert [c[0] for c in calls] == ["exists", "store"]
        assert "pruned" not in result
        assert state.model_fingerprint == "abcd1234"
        assert session.commits == 1 and session.closed

    def test_nothing_pinned_means_nothing_protected(self, calls, monkeypatch):
        player = _Obj(player_id=7)
        session = _Session({route.Player: player, route.PlayerState: None})
        monkeypatch.setattr(route, "get_db_session", lambda: session)

        result = route._store("hash", "ffff", b"model", b"pet", pin=False)

        assert result[route._PROTECT_KEY] == frozenset()
        assert ("store", 7, "ffff", True) in calls, "pet model stored alongside"
        assert session.added and session.added[0].player_id == 7

    def test_outfits_worn_for_personal_bests_are_protected_too(self, calls, monkeypatch):
        # The leaderboards render those models for as long as the time stands;
        # five newer outfits must not be able to erase a record's.
        player = _Obj(player_id=42)
        state = _State(model_fingerprint=None, pinned_model_fingerprint="pinned1")
        session = _Session({
            route.Player: player,
            route.PlayerState: state,
            route.PersonalBestLoadout.model_fingerprint: [("pb_fp_1",), ("pb_fp_2",), (None,)],
        })
        monkeypatch.setattr(route, "get_db_session", lambda: session)

        result = route._store("hash", "abcd1234", b"model", None, pin=False)

        assert result[route._PROTECT_KEY] == frozenset({"pinned1", "pb_fp_1", "pb_fp_2"})

    def test_a_failing_personal_best_lookup_still_protects_the_pin(self, calls, monkeypatch):
        class _Broken(_Session):
            def query(self, model):
                if model is route.PersonalBestLoadout.model_fingerprint:
                    raise RuntimeError("table gone")
                return super().query(model)

        player = _Obj(player_id=42)
        state = _State(model_fingerprint=None, pinned_model_fingerprint="pinned1")
        session = _Broken({route.Player: player, route.PlayerState: state})
        monkeypatch.setattr(route, "get_db_session", lambda: session)

        result = route._store("hash", "abcd1234", b"model", None, pin=False)

        assert result["stored"] is True
        assert result[route._PROTECT_KEY] == frozenset({"pinned1"})

    def test_pin_of_a_new_model_protects_it(self, calls, monkeypatch):
        player = _Obj(player_id=9)
        state = _State(model_fingerprint="old", pinned_model_fingerprint=None)
        session = _Session({route.Player: player, route.PlayerState: state})
        monkeypatch.setattr(route, "get_db_session", lambda: session)

        result = route._store("hash", "0e11", b"model", None, pin=True)

        assert state.pinned_model_fingerprint == "0e11"
        assert result[route._PROTECT_KEY] == frozenset({"0e11"})

    def test_existing_model_is_a_no_op_with_no_carrier_key(self, calls, monkeypatch):
        monkeypatch.setattr(player_model, "model_exists", lambda pid, fp, **kw: True)
        monkeypatch.setattr(player_model, "model_url", lambda pid, fp, **kw: "u")
        player = _Obj(player_id=3)
        session = _Session({route.Player: player, route.PlayerState: None})
        monkeypatch.setattr(route, "get_db_session", lambda: session)

        result = route._store("hash", "abcd", b"model", None)

        assert result == {"stored": False, "player_id": 3, "pinned": False, "url": "u"}
        assert not any(c[0] in ("store", "prune") for c in calls)


class TestFollowUp:
    async def test_prunes_with_protected_set_then_renders(self, calls, monkeypatch):
        rendered = []
        threads = []
        monkeypatch.setattr(
            player_model, "prune_old_models",
            lambda pid, **kw: (threads.append(threading.current_thread()),
                               calls.append(("prune", pid, kw.get("protect")))) and 2)

        async def fake_render(pid, fp):
            rendered.append((pid, fp))
            calls.append(("render", pid, fp))

        monkeypatch.setattr(sys.modules["services.gear_image"], "render_gear_image", fake_render)

        await route._finish_in_background(42, "abcd", frozenset({"keepme"}))

        assert calls == [("prune", 42, frozenset({"keepme"})), ("render", 42, "abcd")]
        # The prune is blocking B2 I/O and must not run on the event loop.
        assert threads and threads[0] is not threading.main_thread()

    async def test_a_failing_prune_does_not_cost_the_render(self, calls, monkeypatch):
        def boom(pid, **kw):
            raise RuntimeError("B2 list failed")

        monkeypatch.setattr(player_model, "prune_old_models", boom)
        rendered = []

        async def fake_render(pid, fp):
            rendered.append((pid, fp))

        monkeypatch.setattr(sys.modules["services.gear_image"], "render_gear_image", fake_render)

        await route._finish_in_background(1, "fp", frozenset())

        assert rendered == [(1, "fp")]

    async def test_a_failing_render_is_swallowed(self, calls, monkeypatch):
        async def bad_render(pid, fp):
            raise RuntimeError("chromium died")

        monkeypatch.setattr(sys.modules["services.gear_image"], "render_gear_image", bad_render)

        await route._finish_in_background(1, "fp")  # must not raise

        assert ("prune", 1, frozenset()) in calls


class TestRoute:
    """End to end through Quart: the response never carries the internal
    protected set, and a stored upload schedules exactly one follow-up."""

    @pytest.fixture
    def app(self):
        from quart import Quart

        app = Quart(__name__)
        app.register_blueprint(route.player_model_bp)
        return app

    async def _post(self, app, **fields):
        from werkzeug.datastructures import FileStorage

        client = app.test_client()
        form = {"acc_hash": "hash", "fingerprint": "abcd1234", **fields}
        files = {"model": FileStorage(io.BytesIO(b"glTF-bytes"), filename="m.glb")}
        return await client.post("/player/model", form=form, files=files)

    async def test_stored_upload_answers_without_carrier_key_and_schedules_followup(
            self, app, monkeypatch):
        scheduled = []

        def fake_store(acc_hash, fingerprint, model_bytes, pet_bytes, pin=False):
            assert model_bytes == b"glTF-bytes" and pet_bytes is None
            return {"stored": True, "player_id": 42, "url": "u", "pinned": pin,
                    route._PROTECT_KEY: frozenset({"pinned1"})}

        async def fake_followup(player_id, fingerprint, protect=frozenset()):
            scheduled.append((player_id, fingerprint, protect))

        monkeypatch.setattr(route, "_store", fake_store)
        monkeypatch.setattr(route, "_finish_in_background", fake_followup)

        resp = await self._post(app)
        body = await resp.get_json()
        # Let the create_task'd follow-up run.
        await asyncio.sleep(0)

        assert resp.status_code == 200
        assert body == {"accepted": True, "stored": True, "player_id": 42,
                        "url": "u", "pinned": False}
        assert scheduled == [(42, "abcd1234", frozenset({"pinned1"}))]

    async def test_repeat_upload_schedules_nothing(self, app, monkeypatch):
        scheduled = []

        def fake_store(*args, **kwargs):
            return {"stored": False, "player_id": 42, "url": "u", "pinned": False}

        async def fake_followup(*args):
            scheduled.append(args)

        monkeypatch.setattr(route, "_store", fake_store)
        monkeypatch.setattr(route, "_finish_in_background", fake_followup)

        resp = await self._post(app)
        await asyncio.sleep(0)

        assert resp.status_code == 200
        assert (await resp.get_json())["stored"] is False
        assert scheduled == []
