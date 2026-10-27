"""Hardcore Worlds: routed apart from the main game and stored on their own.

The plugin (6.1.19+) tags these submissions ``world_type=HARDCORE_MAIN``.
These tests pin that dispatch folds that into ``hardcore`` and sends every
type to ``data/submissions/hardcore.py`` rather than the main processors, and
that the storage path writes only the hardcore tables, keeps the client's
drop value, strips the auth key and is idempotent per GUID.

``db.models`` is stubbed in the unit suite, so the real model file is loaded
onto a private declarative Base and an in-memory SQLite database that holds
ONLY the hardcore tables: any write to a main-game table would fail outright.
"""
import importlib.util
import os
import sys
import types

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import declarative_base, sessionmaker

from data.submissions import dispatch

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PACKAGE = "_hardcore_models_pkg"


def _load_models():
    package = types.ModuleType(_PACKAGE)
    package.__path__ = []
    base = types.ModuleType(f"{_PACKAGE}.base")
    base.Base = declarative_base()
    sys.modules[_PACKAGE] = package
    sys.modules[f"{_PACKAGE}.base"] = base
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.hardcore", os.path.join(_REPO, "db", "models", "hardcore.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return base.Base, module


Base, models = _load_models()


def _load_processor():
    saved = sys.modules.get("db.models.hardcore")
    sys.modules["db.models.hardcore"] = models
    try:
        spec = importlib.util.spec_from_file_location(
            "data.submissions._hardcore_under_test",
            os.path.join(_REPO, "data", "submissions", "hardcore.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop("db.models.hardcore", None)
        else:
            sys.modules["db.models.hardcore"] = saved


hardcore = _load_processor()


@pytest.fixture
def session():
    engine = sa.create_engine("sqlite://")

    # SAVEPOINTs that actually work on pysqlite.
    @event.listens_for(engine, "connect")
    def _no_implicit_transactions(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _explicit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _drop(guid="g1", **extra):
    data = {
        "type": "drop",
        "source": "Vorkath",
        "item": "Dragonbone necklace",
        "id": "22111",
        "quantity": "1",
        "value": "9500000",
        "killcount": "12",
        "guid": guid,
        "player_name": "HC Fresh",
        "acc_hash": "987654321",
        "auth_key": "secret-key",
        "account_type": "NORMAL",
        "p_v": "6.1.19",
        "world_type": "hardcore",
        "used_api": True,
    }
    data.update(extra)
    return data


# ── Routing ──────────────────────────────────────────────────────────────────

class TestRouting:
    @pytest.mark.parametrize("raw", ["HARDCORE_MAIN", "hardcore_main", " Hardcore_Main ", "hardcore"])
    def test_plugin_tag_folds_to_hardcore(self, raw):
        assert dispatch.normalize_world_type(raw) == dispatch.HARDCORE_WORLD_TYPE

    def test_other_worlds_unchanged(self):
        assert dispatch.normalize_world_type(None) == "main"
        assert dispatch.normalize_world_type("MAIN") == "main"
        assert dispatch.normalize_world_type("SEASONAL") == "seasonal"
        assert dispatch.normalize_world_type("DEADMAN") == "deadman"

    def test_clan_relays_and_settings_are_not_stored(self):
        assert dispatch.HARDCORE_TYPES <= dispatch.SUPPORTED_TYPES
        for excluded in ("clan_chat", "clan_broadcast", "config_snapshot"):
            assert excluded not in dispatch.HARDCORE_TYPES
        for kept in ("drop", "collection_log", "personal_best", "death", "experience", "pvp_kill"):
            assert kept in dispatch.HARDCORE_TYPES

    @pytest.mark.asyncio
    async def test_every_type_goes_to_hardcore_storage_not_a_processor(self, monkeypatch):
        import data.submissions as submissions

        calls = []

        async def fake_store(norm_type, data, session):
            calls.append((norm_type, session))
            return "stored"

        async def forbidden(*_a, **_k):
            raise AssertionError("a main-world processor ran for a hardcore submission")

        for name in dispatch._PROCESSORS.values():
            monkeypatch.setattr(submissions, name, forbidden)
        fake_module = types.ModuleType("data.submissions.hardcore")
        fake_module.store_hardcore_submission = fake_store
        monkeypatch.setitem(sys.modules, "data.submissions.hardcore", fake_module)

        for raw_type in ("drop", "npc", "player_death", "kill_time", "level_up"):
            result = await dispatch.dispatch_submission(
                raw_type, {}, "session", world_type="HARDCORE_MAIN")
            assert result == "stored"
        assert [c[0] for c in calls] == ["drop", "drop", "death", "personal_best", "experience"]

    @pytest.mark.asyncio
    async def test_excluded_types_return_none(self):
        assert await dispatch.dispatch_submission(
            "clan_chat", {}, "session", world_type="hardcore") is None


# ── Storage ──────────────────────────────────────────────────────────────────

class TestStorage:
    @pytest.mark.asyncio
    async def test_drop_is_stored_at_the_clients_value(self, session):
        response = await hardcore.store_hardcore_submission("drop", _drop(quantity="2"), session)
        session.commit()
        assert response.success

        player = session.query(models.HardcorePlayer).one()
        assert (player.account_hash, player.player_name) == ("987654321", "HC Fresh")
        assert player.account_type == "normal"
        assert player.plugin_version == "6.1.19"

        drop = session.query(models.HardcoreDrop).one()
        assert drop.player_id == player.id
        assert (drop.item_id, drop.item_name, drop.npc_name) == (22111, "Dragonbone necklace", "Vorkath")
        assert (drop.quantity, drop.unit_value, drop.total_value) == (2, 9_500_000, 19_000_000)
        assert drop.kill_count == 12

        submission = session.query(models.HardcoreSubmission).one()
        assert drop.submission_id == submission.id
        assert submission.submission_type == "drop"
        assert submission.unique_id == "g1"
        assert submission.used_api is True
        assert submission.partition == submission.date_added.year * 100 + submission.date_added.month

    @pytest.mark.asyncio
    async def test_auth_key_is_never_stored(self, session):
        await hardcore.store_hardcore_submission("drop", _drop(), session)
        session.commit()
        payload = session.query(models.HardcoreSubmission).one().payload
        assert "auth_key" not in payload
        assert payload["item"] == "Dragonbone necklace"

    @pytest.mark.asyncio
    async def test_replayed_guid_is_a_no_op(self, session):
        assert (await hardcore.store_hardcore_submission("drop", _drop(), session)).success
        session.commit()
        again = await hardcore.store_hardcore_submission("drop", _drop(), session)
        session.commit()
        assert not again.success
        assert session.query(models.HardcoreSubmission).count() == 1
        assert session.query(models.HardcoreDrop).count() == 1

    @pytest.mark.asyncio
    async def test_other_types_are_stored_raw_without_a_drop_row(self, session):
        death = {"type": "death", "player_name": "HC Fresh", "acc_hash": "987654321",
                 "guid": "d1", "killer": "Vorkath", "location": "Ungael"}
        response = await hardcore.store_hardcore_submission("death", death, session)
        session.commit()
        assert response.success
        submission = session.query(models.HardcoreSubmission).one()
        assert submission.submission_type == "death"
        assert submission.payload["killer"] == "Vorkath"
        assert session.query(models.HardcoreDrop).count() == 0

    @pytest.mark.asyncio
    async def test_one_character_per_account_hash_follows_renames(self, session):
        await hardcore.store_hardcore_submission("drop", _drop("g1"), session)
        await hardcore.store_hardcore_submission("drop", _drop("g2", player_name="HC Renamed"), session)
        session.commit()
        player = session.query(models.HardcorePlayer).one()
        assert player.player_name == "HC Renamed"
        assert session.query(models.HardcoreDrop).count() == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [{"quantity": "0"}, {"quantity": "lots"},
                                     {"value": "2000000000", "quantity": "100"},
                                     {"acc_hash": ""}, {"player_name": " "},
                                     {"item": None}])
    async def test_rejections_store_nothing(self, session, bad):
        response = await hardcore.store_hardcore_submission("drop", _drop(**bad), session)
        session.commit()
        assert not response.success
        assert session.query(models.HardcoreSubmission).count() == 0
        assert session.query(models.HardcoreDrop).count() == 0

    @pytest.mark.asyncio
    async def test_missing_or_junk_value_stores_zero(self, session):
        await hardcore.store_hardcore_submission("drop", _drop("g1", value=None), session)
        await hardcore.store_hardcore_submission("drop", _drop("g2", value="-5"), session)
        session.commit()
        assert [d.total_value for d in session.query(models.HardcoreDrop)] == [0, 0]
