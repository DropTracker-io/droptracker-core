"""Hardcore Worlds: routed like seasonal, archived first, settings per group.

The plugin (6.1.19+) tags these submissions ``world_type=HARDCORE_MAIN``.
These tests pin that:

* dispatch folds that into ``hardcore``, archives every hardcore submission,
  then runs the types with mirror tables through their normal processors with
  ``world_type="hardcore"`` (and never the main-world call);
* the archive strips the auth key and is idempotent per GUID;
* processors find hardcore mirror tables, dedupe keys and settings prefix;
* a group's ``hardcore_`` settings read its main ones when it shares them
  (utils.group_config._resolve_key), and the registry knows every mirror.

``db.models`` is stubbed in the unit suite, so the real model file is loaded
onto a private declarative Base that holds a bare ``players`` table and the
hardcore tables, on in-memory SQLite.
"""
import importlib.util
import os
import sys
import types
from types import SimpleNamespace

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
    # Only what the hardcore tables' foreign keys point at.
    for table, pk in (("players", "player_id"), ("items", "item_id"), ("npc_list", "npc_id")):
        sa.Table(table, base.Base.metadata, sa.Column(pk, sa.Integer, primary_key=True))
    sys.modules[_PACKAGE] = package
    sys.modules[f"{_PACKAGE}.base"] = base
    spec = importlib.util.spec_from_file_location(
        f"{_PACKAGE}.hardcore", os.path.join(_REPO, "db", "models", "hardcore.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return base.Base, module


Base, models = _load_models()


def _load_archive():
    """data/submissions/hardcore.py against the real HardcoreSubmission."""
    import db
    saved = getattr(db, "HardcoreSubmission", None)
    db.HardcoreSubmission = models.HardcoreSubmission
    try:
        spec = importlib.util.spec_from_file_location(
            "data.submissions._hardcore_under_test",
            os.path.join(_REPO, "data", "submissions", "hardcore.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        db.HardcoreSubmission = saved


hardcore = _load_archive()


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
    s.execute(sa.text("INSERT INTO players (player_id) VALUES (7)"))
    yield s
    s.close()


@pytest.fixture
def character(monkeypatch):
    monkeypatch.setattr(hardcore, "ensure_hardcore_player",
                        lambda session, name, acc_hash: SimpleNamespace(player_id=7) if acc_hash else None)


def _drop(guid="g1", **extra):
    data = {
        "type": "drop", "source": "Vorkath", "item": "Dragonbone necklace", "id": "22111",
        "quantity": "1", "value": "9500000", "guid": guid, "player_name": "HC Fresh",
        "acc_hash": "987654321", "auth_key": "secret-key", "p_v": "6.1.19",
        "world_type": "hardcore",
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

    def test_type_sets(self):
        assert dispatch.HARDCORE_TYPES <= dispatch.SUPPORTED_TYPES
        for excluded in ("clan_chat", "clan_broadcast", "config_snapshot"):
            assert excluded not in dispatch.HARDCORE_TYPES
        # Processed exactly where a seasonal mirror (and world_type) exists.
        assert dispatch.HARDCORE_PROCESSED_TYPES == dispatch.SEASONAL_TYPES
        for archived_only in ("experience", "adventure_log", "pvp_kill"):
            assert archived_only in dispatch.HARDCORE_TYPES - dispatch.HARDCORE_PROCESSED_TYPES

    @pytest.fixture
    def wired(self, monkeypatch):
        import data.submissions as submissions
        from data.submissions.common import SubmissionResponse

        calls = {"archived": [], "processed": []}
        archive_ok = {"value": True}

        async def fake_archive(norm_type, data, session):
            calls["archived"].append(norm_type)
            return SubmissionResponse(archive_ok["value"], "archived")

        def make(name):
            async def _proc(data, external_session=None, world_type="main"):
                assert world_type == "hardcore", f"{name} ran as {world_type}"
                calls["processed"].append(name)
                return "processed"
            return _proc

        for name in dispatch._PROCESSORS.values():
            monkeypatch.setattr(submissions, name, make(name))
        fake_module = types.ModuleType("data.submissions.hardcore")
        fake_module.archive_hardcore_submission = fake_archive
        monkeypatch.setitem(sys.modules, "data.submissions.hardcore", fake_module)
        return calls, archive_ok

    @pytest.mark.asyncio
    async def test_processed_types_are_archived_then_processed_as_hardcore(self, wired):
        calls, _ = wired
        for raw_type in ("drop", "npc", "player_death", "kill_time"):
            assert await dispatch.dispatch_submission(
                raw_type, {}, "session", world_type="HARDCORE_MAIN") == "processed"
        assert calls["archived"] == ["drop", "drop", "death", "personal_best"]
        assert calls["processed"] == ["drop_processor", "drop_processor", "death_processor", "pb_processor"]

    @pytest.mark.asyncio
    async def test_archive_only_types_never_reach_a_processor(self, wired):
        calls, _ = wired
        result = await dispatch.dispatch_submission("level_up", {}, "session", world_type="hardcore")
        assert result.success and calls["archived"] == ["experience"] and calls["processed"] == []

    @pytest.mark.asyncio
    async def test_a_refused_archive_stops_processing(self, wired):
        calls, archive_ok = wired
        archive_ok["value"] = False
        result = await dispatch.dispatch_submission("drop", {}, "session", world_type="hardcore")
        assert not result.success and calls["processed"] == []

    @pytest.mark.asyncio
    async def test_excluded_types_return_none(self, wired):
        assert await dispatch.dispatch_submission("clan_chat", {}, "session", world_type="hardcore") is None


# ── Archive ──────────────────────────────────────────────────────────────────

class TestArchive:
    @pytest.mark.asyncio
    async def test_archives_without_the_auth_key(self, session, character):
        response = await hardcore.archive_hardcore_submission("drop", _drop(), session)
        session.commit()
        assert response.success
        row = session.query(models.HardcoreSubmission).one()
        assert (row.player_id, row.submission_type, row.unique_id, row.plugin_version) == (7, "drop", "g1", "6.1.19")
        assert "auth_key" not in row.payload and row.payload["item"] == "Dragonbone necklace"
        assert row.partition == row.date_added.year * 100 + row.date_added.month

    @pytest.mark.asyncio
    async def test_replayed_guid_is_a_no_op(self, session, character):
        assert (await hardcore.archive_hardcore_submission("drop", _drop(), session)).success
        again = await hardcore.archive_hardcore_submission("drop", _drop(), session)
        session.commit()
        assert not again.success
        assert session.query(models.HardcoreSubmission).count() == 1

    @pytest.mark.asyncio
    async def test_no_character_no_row(self, session, character):
        response = await hardcore.archive_hardcore_submission("drop", _drop(acc_hash=""), session)
        assert not response.success
        assert session.query(models.HardcoreSubmission).count() == 0


# ── Processor plumbing ───────────────────────────────────────────────────────

class TestWorldPlumbing:
    def test_side_worlds_and_prefixes(self):
        from data.submissions import common
        assert common.is_side_world("hardcore") and common.is_side_world("seasonal")
        assert not common.is_side_world("main")
        assert common.get_config_prefix("hardcore") == "hardcore_"
        assert common.get_config_prefix("seasonal") == "seasonal_"
        assert common.get_config_prefix("main") == ""

    def test_mirror_models_per_world(self):
        from data.submissions import common
        main = object()
        assert common.get_world_model("main", "drop", main) is main
        for kind in ("drop", "personal_best", "collection_log", "combat_achievement", "pet", "quest"):
            assert common.get_world_model("hardcore", kind) is common._WORLD_MODELS["hardcore"][kind]
            assert common.get_world_model("seasonal", kind) is common._WORLD_MODELS["seasonal"][kind]
            assert common.get_world_model("hardcore", kind) is not common.get_world_model("seasonal", kind)

    def test_every_hardcore_dedupe_type_has_a_table(self):
        from data.submissions import common
        assert set(common._HARDCORE_DEDUP_KINDS.values()) == set(common._WORLD_MODELS["hardcore"])


# ── Group settings ───────────────────────────────────────────────────────────

class TestGroupSettings:
    @pytest.fixture
    def gc(self, monkeypatch):
        from utils import group_config
        group_config._cache.clear()
        stored = {}

        def fake_get_bulk_raw(session, group_ids, keys):
            return {(g, k): stored[(g, k)] for g in group_ids for k in keys if (g, k) in stored}

        def fake_get(session, group_id, key, default=None):
            key = group_config._resolve_key(session, group_id, key)
            return stored.get((group_id, key), default)

        monkeypatch.setattr(group_config, "_get_bulk_raw", fake_get_bulk_raw)
        monkeypatch.setattr(group_config, "get", fake_get)
        monkeypatch.setattr(group_config, "_shares_hardcore_settings",
                            lambda s, g: stored.get((g, group_config.SHARE_HARDCORE_SETTINGS)) == "true")
        return group_config, stored

    def test_separate_settings_read_the_hardcore_key(self, gc):
        group_config, stored = gc
        stored.update({(5, "minimum_value_to_notify"): "1000", (5, "hardcore_minimum_value_to_notify"): "50"})
        assert group_config.get(None, 5, "hardcore_minimum_value_to_notify") == "50"
        bulk = group_config.get_bulk(None, [5], ["hardcore_minimum_value_to_notify"])
        assert bulk == {(5, "hardcore_minimum_value_to_notify"): "50"}

    def test_shared_settings_read_the_main_key_under_the_asked_name(self, gc):
        group_config, stored = gc
        stored.update({(5, "share_hardcore_settings"): "true", (5, "minimum_value_to_notify"): "1000",
                       (5, "hardcore_minimum_value_to_notify"): "50"})
        assert group_config.get(None, 5, "hardcore_minimum_value_to_notify") == "1000"
        bulk = group_config.get_bulk(None, [5], ["hardcore_minimum_value_to_notify", "split_gp_tracking"])
        assert bulk == {(5, "hardcore_minimum_value_to_notify"): "1000"}

    def test_switches_default_off_and_mirrors_inherit_defaults(self, gc):
        group_config, _ = gc
        assert group_config.absent_default("track_hardcore_worlds") == "false"
        assert group_config.absent_default("share_hardcore_settings") == "false"
        assert group_config.absent_default("hardcore_notify_pbs") == group_config.absent_default("notify_pbs")

    def test_registry_mirrors(self):
        from web_api import config_registry as reg
        assert reg.get_config_field("hardcore_channel_id_to_post_loot")["key"] == "channel_id_to_post_loot"
        assert reg.get_config_field("hardcore_notify_pbs")["key"] == "notify_pbs"
        # Not mirrored: no hardcore lootboard theme, no hardcore clan points switch.
        assert reg.get_config_field("hardcore_loot_board_type") is None
        assert "track_hardcore_worlds" in reg.all_config_keys()
        assert "share_hardcore_settings" in reg.all_config_keys()
        for key in reg.HARDCORE_MIRROR_KEYS:
            assert f"hardcore_{key}" in reg.all_config_keys()


# ── Mirrors of special keys (Sentry: "Data too long for column 'config_value'") ─

class TestMirrorsOfSpecialKeys:
    def test_every_mirror_of_an_overflow_key_overflows_too(self):
        # A group saving a long hardcore duplicate-pets list wrote it into the
        # VARCHAR(255) config_value because LONG_VALUE_KEYS was hand-kept.
        from web_api import config_registry as reg
        from web_api.routes import config as routes_config
        for key in routes_config._LONG_VALUE_BASE_KEYS:
            for prefix in (reg.SEASONAL_PREFIX, reg.HARDCORE_PREFIX):
                field = reg.get_config_field(f"{prefix}{key}")
                if field is not None and field["key"] == key:
                    assert f"{prefix}{key}" in routes_config.LONG_VALUE_KEYS
        assert "hardcore_notify_pet_duplicate_pets" in routes_config.LONG_VALUE_KEYS

    def test_direct_readers_follow_the_share_switch(self, monkeypatch):
        from utils import group_config
        shares = {5: True, 6: False}
        monkeypatch.setattr(group_config, "_shares_hardcore_settings", lambda s, g: shares[g])
        assert group_config.resolve_prefix(None, 5, "hardcore_") == ""
        assert group_config.resolve_prefix(None, 6, "hardcore_") == "hardcore_"
        assert group_config.resolve_prefix(None, 5, "seasonal_") == "seasonal_"

    def test_death_filter_reads_hardcore_mirrors(self, monkeypatch):
        # conftest loads the real db/death_filter.py under its dotted name
        # (the ``db`` package itself is a stub, so not ``from db import``).
        death_filter = importlib.import_module("db.death_filter")
        from utils import group_config
        asked = []
        monkeypatch.setattr(group_config, "get", lambda s, g, key, default=None: asked.append(key) or "true")
        assert death_filter._config_flag(None, 5, "notify_deaths_safe", "hardcore")
        assert death_filter._config_flag(None, 5, "notify_deaths_safe", "seasonal")
        assert death_filter._config_flag(None, 5, "notify_deaths_safe", "main")
        assert asked == ["hardcore_notify_deaths_safe", "seasonal_notify_deaths_safe", "notify_deaths_safe"]
