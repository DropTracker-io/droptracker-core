"""The tester-build tables: the models, the migration and the raw SQL agree.

Three things describe ``plugin_test_downloads`` and ``player_plugin_versions``
and nothing but a test makes them say the same thing:

* the ORM models (db/models/tester_builds.py), which the routes query;
* the migration (web126a), which is what production's tables really are;
* the upsert in utils/plugin_versions.py, which is raw SQL so it can be one
  statement, and so names its columns by hand.

A column renamed in one of them is an error on the submission path or a 500
on the tester pages, and neither shows up until it is live. The migration is
run here against in-memory SQLite, never a real database.
"""
from __future__ import annotations

import importlib.util
import os
import re

import pytest
import sqlalchemy as sa

from tests.unit import _tester_db as tdb
from tests.unit._tester_build_models import PlayerPluginVersion, PluginTestDownload
from utils import plugin_versions as pv
from utils import tester_builds as tb

REVISION = "web126a_tester_builds"
MIGRATION_PATH = os.path.join(tdb.REPO_ROOT, "alembic", "versions", f"{REVISION}.py")
TABLES = ("plugin_test_downloads", "player_plugin_versions")
PARENTS = [tdb.User.__table__, tdb.Player.__table__]


@pytest.fixture(scope="module")
def migration():
    pytest.importorskip("alembic", reason="alembic isn't installed in this environment")
    spec = importlib.util.spec_from_file_location("_web126a_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            step()


@pytest.fixture()
def migrated(migration):
    engine = sa.create_engine("sqlite://")
    tdb.Base.metadata.create_all(engine, tables=PARENTS)
    _run(engine, migration.upgrade)
    return engine


@pytest.fixture()
def modelled():
    engine = sa.create_engine("sqlite://")
    tdb.Base.metadata.create_all(
        engine, tables=PARENTS + [PluginTestDownload.__table__, PlayerPluginVersion.__table__])
    return engine


def _shape(engine, table):
    inspector = sa.inspect(engine)
    return {
        "columns": {c["name"]: (str(c["type"]), c["nullable"], c["primary_key"])
                    for c in inspector.get_columns(table)},
        "indexes": {i["name"]: (tuple(i["column_names"]), bool(i["unique"]))
                    for i in inspector.get_indexes(table)},
        "foreign_keys": sorted((tuple(f["constrained_columns"]), f["referred_table"],
                                tuple(f["referred_columns"]))
                               for f in inspector.get_foreign_keys(table)),
    }


class TestMigrationMatchesModels:
    @pytest.mark.parametrize("table", TABLES)
    def test_same_columns_keys_and_indexes(self, migrated, modelled, table):
        assert _shape(migrated, table) == _shape(modelled, table)

    def test_the_shape_itself(self, migrated):
        downloads = _shape(migrated, "plugin_test_downloads")
        assert downloads["columns"] == {
            "id": ("INTEGER", False, 1),
            "user_id": ("INTEGER", False, 0),
            "build_id": ("VARCHAR(96)", False, 0),
            "plugin_version": ("VARCHAR(32)", True, 0),
            "commit_sha": ("VARCHAR(40)", True, 0),
            "runelite_version": ("VARCHAR(32)", True, 0),
            "file_name": ("VARCHAR(160)", True, 0),
            "created_at": ("DATETIME", False, 0),
        }
        assert downloads["indexes"] == {
            "idx_ptd_user_created": (("user_id", "created_at"), False),
            "idx_ptd_build": (("build_id",), False),
        }
        assert downloads["foreign_keys"] == [(("user_id",), "users", ("user_id",))]

        versions = _shape(migrated, "player_plugin_versions")
        assert versions["columns"] == {
            "player_id": ("INTEGER", False, 1),
            "version": ("VARCHAR(32)", False, 2),
            "first_seen": ("DATETIME", False, 0),
            "last_seen": ("DATETIME", False, 0),
            "sightings": ("INTEGER", False, 0),
            "prerelease": ("BOOLEAN", False, 0),
        }
        assert versions["indexes"] == {
            "idx_ppv_version_seen": (("version", "last_seen"), False)}
        assert versions["foreign_keys"] == [(("player_id",), "players", ("player_id",))]

    def test_server_defaults_cover_a_raw_insert(self, migrated):
        # The submission path writes with raw SQL, so the database has to
        # supply whatever the statement leaves out.
        with migrated.begin() as conn:
            conn.exec_driver_sql("INSERT INTO users (user_id, auth_token) VALUES (5, 'x')")
            conn.exec_driver_sql("INSERT INTO players (player_id) VALUES (50)")
            conn.exec_driver_sql(
                "INSERT INTO plugin_test_downloads (user_id, build_id) VALUES (5, 'b1')")
            conn.exec_driver_sql(
                "INSERT INTO player_plugin_versions (player_id, version, first_seen, last_seen) "
                "VALUES (50, '6.0.19', '2026-10-03 15:00:00', '2026-10-03 15:00:00')")
            created_at = conn.exec_driver_sql(
                "SELECT created_at FROM plugin_test_downloads").scalar()
            sightings, prerelease = conn.exec_driver_sql(
                "SELECT sightings, prerelease FROM player_plugin_versions").one()
        assert created_at is not None
        assert (sightings, prerelease) == (1, 0)


class TestMigrationIsSafeToRerun:
    def test_upgrade_twice(self, migration, migrated):
        _run(migrated, migration.upgrade)
        assert set(TABLES) <= set(sa.inspect(migrated).get_table_names())

    def test_one_table_already_there(self, migration, migrated, modelled):
        with migrated.begin() as conn:
            conn.exec_driver_sql("DROP TABLE player_plugin_versions")
        _run(migrated, migration.upgrade)
        for table in TABLES:
            assert _shape(migrated, table) == _shape(modelled, table)

    def test_downgrade_drops_both_and_nothing_else(self, migration, migrated):
        _run(migrated, migration.downgrade)
        assert sorted(sa.inspect(migrated).get_table_names()) == ["players", "users"]

    def test_revision_matches_its_file_name(self, migration):
        assert migration.revision == REVISION
        assert isinstance(migration.down_revision, str) and migration.down_revision


class TestRawSqlNamesRealColumns:
    def test_the_sighting_upsert(self):
        sql = pv._UPSERT_SQL
        table, columns = re.match(r"INSERT INTO (\w+) \(([^)]*)\)", sql).groups()
        assert table == PlayerPluginVersion.__tablename__
        assert ([name.strip() for name in columns.split(",")]
                == [column.name for column in PlayerPluginVersion.__table__.columns])
        # One value per column, in the same order.
        select = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
        assert [value.strip() for value in select.split(",")] == [
            "p.player_id", ":version", "NOW()", "NOW()", "1", ":pre"]
        assert "FROM players p WHERE p.account_hash = :h" in sql
        assert {"player_id", "account_hash"} <= set(tdb.Player.__table__.columns.keys())

    def test_length_limits_match_the_columns(self):
        downloads = PluginTestDownload.__table__.columns
        assert tb.MAX_BUILD_ID_LENGTH == downloads["build_id"].type.length
        assert tb.MAX_ZIP_NAME_LENGTH == downloads["file_name"].type.length
        assert pv.MAX_VERSION_LENGTH == PlayerPluginVersion.__table__.columns["version"].type.length
        # The routes cut these to fit; the numbers there are these columns'.
        assert downloads["plugin_version"].type.length == 32
        assert downloads["runelite_version"].type.length == 32
        assert downloads["commit_sha"].type.length == 40
