"""``POST /player/model/check`` — the question the client asks before exporting.

Why it exists: measured 2026-09-14, ~264k model uploads a day and only ~16-24%
of them were outfits we did not already hold. The plugin remembered a single
fingerprint, so alternating between two outfits re-sent one of them on every
switch — a 53 KB median model read off the player's connection and discarded,
after a mesh export on their game thread.

Two properties this endpoint has to hold, both pinned below:

* a "yes" must also record what the player is *wearing*, because that is the
  only useful thing the wasteful re-upload did; and
* it must never write when nothing changed — a player standing still in known
  gear asks this repeatedly.

The route module is loaded from its file path so the conftest's stubs for
``api.core`` and ``db.models`` stand in for the DB, matching
``test_player_model_upload_followup``.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


route = _load("_player_model_check_under_test", "api", "routes", "player_model.py")
player_model = sys.modules["services.player_model"]


class _Query:
    def __init__(self, result):
        self._result = result

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._result


class _Session:
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
    """Stands in for the ORM PlayerState, whose attributes would otherwise read
    back as mocks under the conftest stub."""

    player_id = None
    model_fingerprint = None
    pinned_model_fingerprint = None

    def __init__(self, player_id=None, model_fingerprint=None):
        self.player_id = player_id
        self.model_fingerprint = model_fingerprint


@pytest.fixture(autouse=True)
def _real_state_class(monkeypatch):
    monkeypatch.setattr(route, "PlayerState", _State)


@pytest.fixture
def held(monkeypatch):
    """Fingerprints we hold, as ``{(player_id, fingerprint, pet): True}``.

    Returns the call log so a test can assert we did not ask about a pet for an
    outfit we do not have.
    """
    log = []
    store = {}

    def model_exists(player_id, fingerprint, **kw):
        pet = kw.get("pet", False)
        log.append((player_id, fingerprint, pet))
        return store.get((player_id, fingerprint, pet), False)

    monkeypatch.setattr(player_model, "model_exists", model_exists)
    monkeypatch.setattr(player_model, "is_valid_fingerprint", lambda fp: fp != "nope!")
    return store, log


def _session(monkeypatch, player, state):
    session = _Session({route.Player: player, route.PlayerState: state})
    monkeypatch.setattr(route, "get_db_session", lambda: session)
    return session


class TestCheck:
    def test_a_held_outfit_answers_yes_and_becomes_the_current_one(self, held, monkeypatch):
        store, _log = held
        store[(42, "abcd1234", False)] = True
        state = _State(player_id=42, model_fingerprint="older_fp")
        session = _session(monkeypatch, _Obj(player_id=42), state)

        result = route._check("hash", "abcd1234")

        assert result == {"has_model": True, "has_pet": False}
        # The switch back into gear we already hold is exactly what the old
        # re-upload told us, and the profile renders this fingerprint.
        assert state.model_fingerprint == "abcd1234"
        assert session.commits == 1 and session.closed

    def test_standing_in_the_same_gear_writes_nothing(self, held, monkeypatch):
        store, _log = held
        store[(42, "abcd1234", False)] = True
        state = _State(player_id=42, model_fingerprint="abcd1234")
        session = _session(monkeypatch, _Obj(player_id=42), state)

        result = route._check("hash", "abcd1234")

        assert result["has_model"] is True
        assert session.commits == 0, "an unchanged outfit must not cost a write"

    def test_a_pet_is_reported_separately(self, held, monkeypatch):
        store, _log = held
        store[(7, "ffff", False)] = True
        store[(7, "ffff", True)] = True
        _session(monkeypatch, _Obj(player_id=7), _State(player_id=7))

        assert route._check("hash", "ffff") == {"has_model": True, "has_pet": True}

    def test_an_outfit_we_lack_answers_no_without_asking_about_its_pet(self, held, monkeypatch):
        _store, log = held
        session = _session(monkeypatch, _Obj(player_id=7), _State(player_id=7))

        result = route._check("hash", "beef")

        assert result == {"has_model": False, "has_pet": False}
        assert log == [(7, "beef", False)], "a pet cannot exist without its model"
        assert session.commits == 0, "we hold nothing, so nothing is current"

    def test_a_missing_state_row_is_created_for_a_held_outfit(self, held, monkeypatch):
        store, _log = held
        store[(9, "abc", False)] = True
        session = _session(monkeypatch, _Obj(player_id=9), None)

        route._check("hash", "abc")

        assert session.added and session.added[0].player_id == 9
        assert session.added[0].model_fingerprint == "abc"

    def test_an_unknown_account_is_not_an_error(self, held, monkeypatch):
        _session(monkeypatch, None, None)

        # The route turns this into 202 accepted:false, the same shape the
        # upload uses — there is nothing the client can fix.
        assert route._check("hash", "abc") is None

    def test_a_malformed_fingerprint_never_reaches_the_database(self, held, monkeypatch):
        _store, log = held

        def explode():
            raise AssertionError("must not open a session for a bad fingerprint")

        monkeypatch.setattr(route, "get_db_session", explode)

        assert route._check("hash", "nope!") == {"has_model": False, "has_pet": False}
        assert log == []
