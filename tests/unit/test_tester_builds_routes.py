"""Tester build routes: who may download, what a download records, the staff view.

What these pin:

* the download is for Bug Testers and site developers, and nobody else: a
  signed-out visitor gets 401, a signed-in non-tester 403 with a code the
  website shows its own notice for;
* a download is recorded once per build per ten minutes, however many times
  a resumed or ranged transfer re-enters the endpoint;
* the endpoint only ever names a file that is safe to serve and is really on
  disk. A manifest edited to point somewhere else gets a 404, not a file;
* the staff view joins roster, downloads and plugin versions seen without
  letting an account hash out.

The queries run for real, against the SQLite harness in _tester_db.py, with
the two tables this feature adds built from the application's own models
(_tester_build_models.py).
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import web_api.deps as deps
import web_api.routes.tester_builds as tbr

from tests.unit import _tester_db as tdb
from tests.unit._tester_build_models import PlayerPluginVersion, PluginTestDownload

COMMIT = "2cd73be4f0a1b2c3d4e5f60718293a4b5c6d7e8f"
RELEASE_COMMIT = "577fa89000000000000000000000000000000000"
CURRENT = "6.0.19-2cd73be-rl1.13.1"
OLDER = "6.0.18-1111111-rl1.13.1"

TESTER, BYSTANDER, STAFF, TESTER_TWO, IDLE_TESTER = 5, 6, 7, 8, 9


def _manifest(build_id=CURRENT, built_at="2026-10-03T15:28:07Z", version="6.0.19", **over):
    manifest = {
        "schema": 1,
        "build_id": build_id,
        "file": f"droptracker-dev-{build_id}.zip",
        "size_bytes": 28563998,
        "sha256": "ab" * 32,
        "built_at": built_at,
        "reason": "plugin",
        "repo_url": "https://github.com/joelhalen/droptracker-plugin",
        "plugin": {"version": version, "commit": COMMIT, "short": "2cd73be", "ref": "master",
                   "committed_at": "2026-10-03T15:14:46+00:00"},
        "runelite_version": "1.13.1",
        "release": {"commit": RELEASE_COMMIT, "short": "577fa89", "version": "6.0.12",
                    "is_ancestor": True},
        "previous_build": None,
        "changes": [{"commit": COMMIT, "short": "2cd73be", "version": version,
                     "title": "Clan chat sync works without the API",
                     "subject": "v6.0.19: ...", "points": ["One", "Two"], "author": "a",
                     "date": "2026-10-03T15:14:46+00:00", "pr": 59}],
        "since_release": [{"commit": COMMIT, "short": "2cd73be", "version": version,
                           "title": "Clan chat sync works without the API", "points": [],
                           "date": "2026-10-03T15:14:46+00:00", "pr": 59}],
        "smoke": {"script": "pass", "jar": "pass"},
    }
    manifest.update(over)
    return manifest


@pytest.fixture()
def builds(tmp_path, monkeypatch):
    """An empty publish directory, and a way to publish into it."""
    monkeypatch.setenv("TESTER_BUILDS_DIR", str(tmp_path))
    (tmp_path / "builds").mkdir()

    def publish(manifest, current=True, with_zip=True, listed=True):
        if listed:
            (tmp_path / "builds" / f"{manifest['build_id']}.json").write_text(json.dumps(manifest))
        if with_zip:
            (tmp_path / "builds" / manifest["file"]).write_bytes(b"PK zip bytes")
        if current:
            (tmp_path / "current.json").write_text(json.dumps(manifest))
        return manifest

    return SimpleNamespace(root=tmp_path, publish=publish)


@pytest.fixture()
def db(monkeypatch):
    """The harness database with one tester, one bystander and one developer."""
    # One connection for every thread. The routes run their queries through
    # asyncio.to_thread, and an in-memory SQLite database is per connection.
    engine = sa.create_engine("sqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    tdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    tdb.install_models(monkeypatch, factory)
    monkeypatch.setattr(tbr, "PluginTestDownload", PluginTestDownload)
    monkeypatch.setattr(tbr, "PlayerPluginVersion", PlayerPluginVersion)
    monkeypatch.setattr(tbr, "User", tdb.User)

    @contextmanager
    def _db_session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    def _load_user(s, user_id):
        # The harness users table has no role columns; the developer is named here.
        return SimpleNamespace(user_id=user_id, username="row", is_superadmin=False,
                               is_developer=(user_id == STAFF))

    monkeypatch.setattr(tbr, "db_session", _db_session)
    monkeypatch.setattr(tbr, "load_user", _load_user)

    with factory() as s:
        s.add_all([
            tdb.User(user_id=TESTER, discord_id="111", username="tester", auth_token="a" * 16),
            tdb.User(user_id=BYSTANDER, discord_id="222", username="bystander", auth_token="b" * 16),
            tdb.User(user_id=STAFF, discord_id="333", username="dev", auth_token="c" * 16),
        ])
        s.add_all([
            tdb.Player(player_id=50, player_name="Main", account_hash="-900", wom_id=500,
                       user_id=TESTER),
            tdb.Player(player_id=51, player_name="Alt", account_hash="-901", wom_id=501,
                       user_id=TESTER),
            tdb.Player(player_id=60, player_name="Other", account_hash="-902", wom_id=600,
                       user_id=BYSTANDER),
        ])
        s.flush()
        badge = tdb.add_badge(s)
        tdb.award(s, badge, 51)                       # on the alt: any account counts
        tdb.award(s, badge, 60, status="revoked")
        s.commit()
    return factory


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


@pytest.fixture()
def sign_in(monkeypatch):
    def _sign_in(user_id):
        monkeypatch.setattr(tbr, "current_user_id", lambda: user_id)

    return _sign_in


@pytest.fixture()
def roster(monkeypatch):
    """The real services.tester_roster, importable under the stubbed package
    (the staff view lazy-imports it)."""
    return tdb.load("_tester_roster_for_build_routes", "services", "tester_roster.py",
                    register_as="services.tester_roster", monkeypatch=monkeypatch)


def _downloads(factory, **filters):
    with factory() as s:
        return (s.query(PluginTestDownload).filter_by(**filters)
                .order_by(PluginTestDownload.id).all())


def _add_download(factory, user_id, build_id=CURRENT, ago=timedelta(0), version="6.0.19"):
    with factory() as s:
        s.add(PluginTestDownload(user_id=user_id, build_id=build_id, plugin_version=version,
                                 created_at=datetime.now() - ago))
        s.commit()


# ── Who gets in ───────────────────────────────────────────────────────────────

class TestGate:
    async def test_signed_out_is_401(self, client, db, builds):
        builds.publish(_manifest())
        for method, path in (("get", "/api/v1/tester-builds"),
                             ("post", "/api/v1/tester-builds/download"),
                             ("get", "/api/v1/admin/tester-builds")):
            resp = await getattr(client, method)(path)
            assert resp.status_code == 401, path
            assert (await resp.get_json())["code"] == "auth_required"
        assert _downloads(db) == []

    async def test_non_tester_is_403_with_a_code_the_site_can_branch_on(
            self, client, db, builds, sign_in):
        builds.publish(_manifest())
        sign_in(BYSTANDER)   # holds the badge only as a revoked award
        for method, path in (("get", "/api/v1/tester-builds"),
                             ("post", "/api/v1/tester-builds/download")):
            resp = await getattr(client, method)(path)
            assert resp.status_code == 403, path
            body = await resp.get_json()
            assert body["code"] == "bug_tester_required"
            assert body["detail"] == "This download is for Bug Testers."
        assert _downloads(db) == []

    async def test_an_inactive_badge_makes_nobody_a_tester(self, client, db, builds, sign_in):
        builds.publish(_manifest())
        with db() as s:
            s.query(tdb.Badge).update({"active": False})
            s.commit()
        sign_in(TESTER)
        resp = await client.get("/api/v1/tester-builds")
        assert resp.status_code == 403
        assert (await resp.get_json())["code"] == "bug_tester_required"

    async def test_staff_are_allowed_without_the_badge(self, client, db, builds, sign_in):
        builds.publish(_manifest())
        sign_in(STAFF)
        resp = await client.get("/api/v1/tester-builds")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["is_staff"] is True and body["current"]["build_id"] == CURRENT

        resp = await client.post("/api/v1/tester-builds/download")
        assert resp.status_code == 200
        assert [d.user_id for d in _downloads(db)] == [STAFF]

    async def test_the_staff_view_is_for_developers_only(
            self, client, db, builds, sign_in, roster):
        sign_in(TESTER)      # a real tester, but not a developer
        resp = await client.get("/api/v1/admin/tester-builds")
        assert resp.status_code == 403
        assert (await resp.get_json())["code"] == "developer_required"


# ── GET /tester-builds ────────────────────────────────────────────────────────

class TestOverview:
    async def test_tester_gets_the_current_build_and_their_downloads(
            self, client, db, builds, sign_in):
        builds.publish(_manifest(OLDER, "2026-10-01T10:00:00Z", version="6.0.18"), current=False)
        builds.publish(_manifest())
        _add_download(db, TESTER, OLDER, ago=timedelta(days=2), version="6.0.18")
        _add_download(db, STAFF, CURRENT, ago=timedelta(hours=1))    # someone else's
        sign_in(TESTER)

        resp = await client.get("/api/v1/tester-builds")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "private, no-store"
        body = await resp.get_json()
        assert set(body) == {"current", "recent", "my_downloads", "has_current", "is_staff"}
        assert body["is_staff"] is False

        current = body["current"]
        assert current["build_id"] == CURRENT and current["version"] == "6.0.19"
        assert current["commit"] == COMMIT and current["runelite_version"] == "1.13.1"
        assert current["built_at"] == int(
            datetime(2026, 10, 3, 15, 28, 7, tzinfo=timezone.utc).timestamp())
        assert current["release"]["version"] == "6.0.12"
        assert current["changes"][0]["title"] == "Clan chat sync works without the API"
        assert len(current["since_release"]) == 1
        assert "file" not in current

        assert [b["build_id"] for b in body["recent"]] == [OLDER]
        assert body["recent"][0]["since_release"] == []
        assert len(body["recent"][0]["changes"]) == 1

        assert body["has_current"] is False
        assert len(body["my_downloads"]) == 1
        mine = body["my_downloads"][0]
        assert mine["build_id"] == OLDER and mine["version"] == "6.0.18"
        assert isinstance(mine["downloaded_at"], int)

        await client.post("/api/v1/tester-builds/download")
        body = await (await client.get("/api/v1/tester-builds")).get_json()
        assert body["has_current"] is True
        assert [d["build_id"] for d in body["my_downloads"]] == [CURRENT, OLDER]

    async def test_nothing_published_yet(self, client, db, builds, sign_in):
        sign_in(TESTER)
        resp = await client.get("/api/v1/tester-builds")
        assert resp.status_code == 200
        assert await resp.get_json() == {
            "current": None, "recent": [], "my_downloads": [],
            "has_current": False, "is_staff": False,
        }

    async def test_recent_is_the_eight_builds_before_the_current_one(
            self, client, db, builds, sign_in):
        for day in range(1, 12):     # eleven older builds
            builds.publish(_manifest(f"6.0.{day}-abcdef0-rl1", f"2026-09-{day:02d}T00:00:00Z",
                                     version=f"6.0.{day}"), current=False)
        builds.publish(_manifest())
        # Built after the current one but never made current: not an earlier build.
        builds.publish(_manifest("6.0.20-fffffff-rl1", "2026-10-04T00:00:00Z",
                                 version="6.0.20"), current=False)
        sign_in(TESTER)
        body = await (await client.get("/api/v1/tester-builds")).get_json()
        assert [b["version"] for b in body["recent"]] == [
            f"6.0.{day}" for day in range(11, 3, -1)]

    async def test_my_downloads_are_the_newest_ten(self, client, db, builds, sign_in):
        builds.publish(_manifest())
        for n in range(12):
            _add_download(db, TESTER, f"build-{n}", ago=timedelta(hours=12 - n))
        sign_in(TESTER)
        body = await (await client.get("/api/v1/tester-builds")).get_json()
        assert [d["build_id"] for d in body["my_downloads"]] == [
            f"build-{n}" for n in range(11, 1, -1)]


# ── POST /tester-builds/download ──────────────────────────────────────────────

class TestDownload:
    async def test_records_the_download_and_names_a_safe_file(self, client, db, builds, sign_in):
        manifest = builds.publish(_manifest())
        sign_in(TESTER)
        resp = await client.post("/api/v1/tester-builds/download")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "private, no-store"
        body = await resp.get_json()
        assert body == {
            "file": manifest["file"], "filename": manifest["file"], "build_id": CURRENT,
            "version": "6.0.19", "size_bytes": 28563998,
        }
        assert tbr.tb.SAFE_ZIP_RE.fullmatch(body["file"])

        (row,) = _downloads(db)
        assert (row.user_id, row.build_id, row.plugin_version) == (TESTER, CURRENT, "6.0.19")
        assert (row.commit_sha, row.runelite_version) == (COMMIT, "1.13.1")
        assert row.file_name == manifest["file"]
        assert abs((datetime.now() - row.created_at).total_seconds()) < 60

    async def test_a_repeat_within_ten_minutes_is_the_same_download(
            self, client, db, builds, sign_in):
        builds.publish(_manifest())
        sign_in(TESTER)
        for _ in range(4):   # a resumed, ranged transfer re-enters the endpoint
            resp = await client.post("/api/v1/tester-builds/download")
            assert resp.status_code == 200
        assert len(_downloads(db)) == 1

        # Someone else downloading the same build is their own download.
        sign_in(STAFF)
        await client.post("/api/v1/tester-builds/download")
        assert [d.user_id for d in _downloads(db)] == [TESTER, STAFF]

    async def test_after_ten_minutes_it_counts_again(self, client, db, builds, sign_in):
        builds.publish(_manifest())
        _add_download(db, TESTER, CURRENT, ago=timedelta(minutes=9))
        sign_in(TESTER)
        await client.post("/api/v1/tester-builds/download")
        assert len(_downloads(db)) == 1

        with db() as s:
            s.query(PluginTestDownload).update(
                {"created_at": datetime.now() - timedelta(minutes=11)})
            s.commit()
        await client.post("/api/v1/tester-builds/download")
        assert len(_downloads(db)) == 2

    async def test_a_new_build_is_a_new_download(self, client, db, builds, sign_in):
        builds.publish(_manifest(OLDER, "2026-10-01T10:00:00Z", version="6.0.18"))
        sign_in(TESTER)
        await client.post("/api/v1/tester-builds/download")
        builds.publish(_manifest())
        await client.post("/api/v1/tester-builds/download")
        assert [d.build_id for d in _downloads(db)] == [OLDER, CURRENT]

    async def test_no_current_build_is_404(self, client, db, builds, sign_in):
        sign_in(TESTER)
        resp = await client.post("/api/v1/tester-builds/download")
        assert resp.status_code == 404
        assert (await resp.get_json())["code"] == "no_build"
        assert _downloads(db) == []

    async def test_zip_gone_from_disk_is_404(self, client, db, builds, sign_in):
        builds.publish(_manifest(), with_zip=False)
        sign_in(TESTER)
        resp = await client.post("/api/v1/tester-builds/download")
        assert resp.status_code == 404
        assert (await resp.get_json())["code"] == "no_build"
        assert _downloads(db) == []

    @pytest.mark.parametrize("name", ["../secret.zip", "sub/inner.zip", "evil.zip\nX-Evil: 1",
                                      "notes.txt"])
    async def test_unsafe_file_name_is_404_and_never_echoed(
            self, client, db, builds, sign_in, name):
        # Each of these exists, so only the name check stands in the way.
        (builds.root / "secret.zip").write_bytes(b"outside builds/")
        (builds.root / "builds" / "sub").mkdir()
        (builds.root / "builds" / "sub" / "inner.zip").write_bytes(b"nested")
        (builds.root / "builds" / "notes.txt").write_bytes(b"not a zip")
        builds.publish(_manifest(file=name), with_zip=False)
        sign_in(TESTER)
        resp = await client.post("/api/v1/tester-builds/download")
        assert resp.status_code == 404
        raw = await resp.get_data(as_text=True)
        assert json.loads(raw)["code"] == "no_build"
        assert "secret" not in raw and "inner" not in raw and "Evil" not in raw
        assert _downloads(db) == []

    async def test_size_falls_back_to_the_file_on_disk(self, client, db, builds, sign_in):
        builds.publish(_manifest(size_bytes=None))
        sign_in(TESTER)
        body = await (await client.post("/api/v1/tester-builds/download")).get_json()
        assert body["size_bytes"] == len(b"PK zip bytes")


# ── GET /admin/tester-builds ──────────────────────────────────────────────────

def _seen(factory, player_id, version, first_ago, last_ago, prerelease):
    now = datetime.now()
    with factory() as s:
        s.add(PlayerPluginVersion(player_id=player_id, version=version,
                                  first_seen=now - first_ago, last_seen=now - last_ago,
                                  sightings=3, prerelease=prerelease))
        s.commit()


class TestAdminView:
    async def test_empty(self, client, db, builds, sign_in, roster):
        sign_in(STAFF)
        resp = await client.get("/api/v1/admin/tester-builds")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "private, no-store"
        body = await resp.get_json()
        assert body["status"]["state"] == "unknown"
        assert body["current"] is None and body["release_version"] is None
        assert body["builds"] == [] and body["recent_downloads"] == []
        (tester,) = body["testers"]
        assert tester == {
            "user_id": TESTER, "discord_id": "111", "username": "tester",
            "players": [{"player_id": 50, "name": "Main"}, {"player_id": 51, "name": "Alt"}],
            "download_count": 0, "last_download": None, "has_current": False,
            "seen": None, "tested_versions": [],
        }

    async def test_pipeline_builds_testers_and_downloads(
            self, client, db, builds, sign_in, roster):
        builds.publish(_manifest(OLDER, "2026-10-01T10:00:00Z", version="6.0.18"), current=False)
        builds.publish(_manifest())
        (builds.root / "status.json").write_text(json.dumps({
            "checked_at": "2026-10-03T15:40:00Z", "state": "ok", "ref": "master",
            "head_commit": COMMIT, "runelite_version": "1.13.1",
            "current_build_id": CURRENT, "failed": None, "error": None,
            "log_tail": "must not be passed on",
        }))
        with db() as s:
            s.add_all([
                tdb.User(user_id=TESTER_TWO, discord_id="444", username="second",
                         auth_token="d" * 16),
                tdb.User(user_id=IDLE_TESTER, discord_id=None, username="idle",
                         auth_token="e" * 16),
                tdb.Player(player_id=80, player_name="Second", account_hash="-980",
                           wom_id=800, user_id=TESTER_TWO),
                tdb.Player(player_id=90, player_name="Idle", account_hash="-990",
                           wom_id=900, user_id=IDLE_TESTER),
            ])
            s.flush()
            badge = s.query(tdb.Badge).one()
            tdb.award(s, badge, 80)
            tdb.award(s, badge, 90)
            s.commit()

        _add_download(db, TESTER, OLDER, ago=timedelta(days=2), version="6.0.18")
        _add_download(db, TESTER, CURRENT, ago=timedelta(hours=1))
        _add_download(db, STAFF, CURRENT, ago=timedelta(minutes=30))

        # The tester: the released version on the main, two dev builds since.
        _seen(db, 50, "6.0.12", timedelta(days=9), timedelta(days=3), False)
        _seen(db, 50, "6.0.18", timedelta(days=2), timedelta(days=2), True)
        _seen(db, 51, "6.0.19", timedelta(minutes=50), timedelta(minutes=20), True)
        # The second tester never took a build, but submitted most recently.
        _seen(db, 80, "6.0.12", timedelta(days=30), timedelta(minutes=10), False)
        # Not a tester: must not show up anywhere.
        _seen(db, 60, "6.0.19", timedelta(minutes=5), timedelta(minutes=1), True)

        sign_in(STAFF)
        resp = await client.get("/api/v1/admin/tester-builds")
        assert resp.status_code == 200
        raw = await resp.get_data(as_text=True)
        body = json.loads(raw)
        assert set(body) == {"status", "current", "release_version", "builds", "testers",
                             "recent_downloads"}

        assert body["status"] == {
            "state": "ok",
            "checked_at": int(datetime(2026, 10, 3, 15, 40, tzinfo=timezone.utc).timestamp()),
            "ref": "master", "head_commit": COMMIT, "runelite_version": "1.13.1",
            "error": None, "current_build_id": CURRENT,
        }
        assert body["current"]["build_id"] == CURRENT
        assert body["release_version"] == "6.0.12"

        assert [(b["build_id"], b["version"], b["downloads"], b["testers"])
                for b in body["builds"]] == [(CURRENT, "6.0.19", 2, 2), (OLDER, "6.0.18", 1, 1)]
        assert set(body["builds"][0]) == {"build_id", "version", "runelite_version",
                                          "built_at", "reason", "downloads", "testers"}

        # Most recently active first; the one with nothing to show comes last.
        assert [t["user_id"] for t in body["testers"]] == [TESTER_TWO, TESTER, IDLE_TESTER]
        second, first, idle = body["testers"]

        assert first["download_count"] == 2 and first["has_current"] is True
        assert first["last_download"]["build_id"] == CURRENT
        assert first["last_download"]["version"] == "6.0.19"
        assert first["seen"]["version"] == "6.0.19"
        assert first["seen"]["player_name"] == "Alt"
        assert first["seen"]["prerelease"] is True
        assert first["seen"]["first_seen"] < first["seen"]["last_seen"]
        assert first["tested_versions"] == ["6.0.19", "6.0.18"]

        assert second["download_count"] == 0 and second["last_download"] is None
        assert second["has_current"] is False
        assert second["seen"]["version"] == "6.0.12" and second["seen"]["prerelease"] is False
        assert second["tested_versions"] == []

        assert idle["discord_id"] is None and idle["seen"] is None
        assert idle["players"] == [{"player_id": 90, "name": "Idle"}]

        assert [(d["user_id"], d["username"], d["build_id"]) for d in body["recent_downloads"]] == [
            (STAFF, "dev", CURRENT), (TESTER, "tester", CURRENT), (TESTER, "tester", OLDER)]
        assert set(body["recent_downloads"][0]) == {"user_id", "username", "build_id",
                                                    "version", "downloaded_at"}

        # The roster carries account hashes; the API must not.
        for secret in ("account_hash", "-900", "-901", "-980", "-990", "auth_token",
                       "must not be passed on"):
            assert secret not in raw
        assert "Other" not in raw    # the bystander's account

    async def test_tested_versions_sort_by_version_not_by_text(
            self, client, db, builds, sign_in, roster):
        for version in ("6.0.9", "6.0.19", "6.0.10", "nightly"):
            _seen(db, 50, version, timedelta(days=1), timedelta(days=1), True)
        sign_in(STAFF)
        body = await (await client.get("/api/v1/admin/tester-builds")).get_json()
        assert body["testers"][0]["tested_versions"] == ["6.0.19", "6.0.10", "6.0.9", "nightly"]


# ── web_api.deps.is_bug_tester ────────────────────────────────────────────────

class TestIsBugTester:
    @pytest.fixture()
    def session(self, monkeypatch):
        factory = tdb.make_sessionmaker()
        tdb.install_models(monkeypatch, factory)
        with factory() as s:
            s.add_all([
                tdb.User(user_id=0, discord_id="100", username="zero", auth_token="z" * 16),
                tdb.User(user_id=5, discord_id="111", username="tester", auth_token="a" * 16),
                tdb.User(user_id=6, discord_id="222", username="bystander", auth_token="b" * 16),
                tdb.Player(player_id=10, player_name="Zero", account_hash="1", wom_id=1, user_id=0),
                tdb.Player(player_id=50, player_name="Main", account_hash="2", wom_id=2, user_id=5),
                tdb.Player(player_id=51, player_name="Alt", account_hash="3", wom_id=3, user_id=5),
                tdb.Player(player_id=60, player_name="Other", account_hash="4", wom_id=4, user_id=6),
                tdb.Player(player_id=70, player_name="Unlinked", account_hash="5", wom_id=5),
            ])
            s.flush()
            yield s

    def test_the_key_matches_the_roster_and_the_role_sync(self):
        roster = tdb.load("_tester_roster_key_pin", "services", "tester_roster.py")
        assert deps.BUG_TESTER_BADGE_KEY == roster.BUG_TESTER_BADGE_KEY == "bug_tester_helper"

    def test_an_active_award_on_any_account(self, session):
        tdb.award(session, tdb.add_badge(session), 51)
        assert deps.is_bug_tester(session, 5) is True
        assert deps.is_bug_tester(session, 6) is False

    def test_user_zero_is_a_real_account(self, session):
        tdb.award(session, tdb.add_badge(session), 10)
        assert deps.is_bug_tester(session, 0) is True
        assert deps.is_bug_tester(session, None) is False

    def test_a_revoked_award_does_not_count(self, session):
        tdb.award(session, tdb.add_badge(session), 50, status="revoked")
        assert deps.is_bug_tester(session, 5) is False

    def test_an_inactive_badge_does_not_count(self, session):
        tdb.award(session, tdb.add_badge(session, active=False), 50)
        assert deps.is_bug_tester(session, 5) is False

    def test_another_badge_does_not_count(self, session):
        tdb.award(session, tdb.add_badge(session, key="daily_champion"), 50)
        assert deps.is_bug_tester(session, 5) is False

    def test_an_unlinked_account_makes_nobody_a_tester(self, session):
        tdb.award(session, tdb.add_badge(session), 70)
        assert deps.is_bug_tester(session, 5) is False
        assert deps.is_bug_tester(session, 0) is False

    def test_no_badge_at_all(self, session):
        assert deps.is_bug_tester(session, 5) is False
