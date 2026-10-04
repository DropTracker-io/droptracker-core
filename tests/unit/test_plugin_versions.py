"""utils/plugin_versions.py: which plugin version each account is running.

record_sighting() sits on the submission path of both transports, so what
matters most is what it costs and what it can break:

* a submission whose (account, version) was claimed in the last six hours
  costs one Redis call and nothing else;
* a claimed one writes exactly one statement, in a session of its own that
  is always closed;
* nothing it does can raise into the submission, and a failed write gives
  the claim back so the next submission retries;
* mirrored production traffic is never recorded on the instance it is
  mirrored to.

Redis and the database are fakes throughout.
"""
from __future__ import annotations

import json

import pytest

from utils import plugin_versions as pv
from utils.mirror_context import mirror_sink


class FakeRedis:
    def __init__(self, claimed=True, fail=False):
        self.claimed = claimed
        self.fail = fail
        self.sets = []
        self.deleted = []

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis is down")
        self.sets.append({"key": key, "value": value, "nx": nx, "ex": ex})
        return True if self.claimed else None

    def delete(self, key):
        self.deleted.append(key)


class FakeSession:
    def __init__(self, fail=False):
        self.fail = fail
        self.executed = []
        self.committed = False
        self.closed = False

    def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        if self.fail:
            raise RuntimeError("Table 'data.player_plugin_versions' doesn't exist")

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture()
def wired(monkeypatch):
    """record_sighting with a claiming Redis, a working database and 6.0.12
    on the Plugin Hub. Returns the fakes, plus the sessions opened."""
    redis = FakeRedis()
    sessions = []

    def _session():
        sessions.append(FakeSession())
        return sessions[-1]

    monkeypatch.setattr(pv, "_redis", lambda: redis)
    monkeypatch.setattr(pv, "_private_session", _session)
    monkeypatch.setattr(pv, "_release_version", lambda: "6.0.12")
    monkeypatch.setattr(pv, "_skip_until", 0.0)
    return redis, sessions


def _submission(**over):
    data = {"acc_hash": "-123456", "p_v": "6.0.19", "player": "Tester", "type": "drop"}
    data.update(over)
    return data


class TestVersionTuple:
    @pytest.mark.parametrize("text, expected", [
        ("6.0.19", (6, 0, 19)),
        ("v6.0.19", (6, 0, 19)),
        ("V6.0.19", (6, 0, 19)),
        ("6.0.6-SNAPSHOT", (6, 0, 6)),
        ("6.0", (6, 0, 0)),
        (" 6.0.19 ", (6, 0, 19)),
        ("6.0.19+build.5", (6, 0, 19)),
        ("10.20.30", (10, 20, 30)),
    ])
    def test_parses(self, text, expected):
        assert pv.version_tuple(text) == expected

    @pytest.mark.parametrize("text", [
        None, "", "unknown", "six", "6", "6.", ".6.0", "6.x.1", "6.0.19abc", "6.0.19.5",
        "6.0.rc1", "v", 6.0, 6, ["6.0.19"], "6.0.19\n7.0.0", "1234567.0.0",
    ])
    def test_junk(self, text):
        assert pv.version_tuple(text) is None

    def test_orders_numerically_not_as_text(self):
        assert pv.version_tuple("6.0.19") > pv.version_tuple("6.0.9")
        assert pv.version_tuple("6.1") > pv.version_tuple("6.0.99")


class TestIsPrerelease:
    def test_ahead_of_the_release(self):
        assert pv.is_prerelease("6.0.19", "6.0.12") is True
        assert pv.is_prerelease("v6.1", "6.0.12") is True

    def test_at_or_behind_the_release(self):
        assert pv.is_prerelease("6.0.12", "6.0.12") is False
        assert pv.is_prerelease("6.0.6-SNAPSHOT", "6.0.6") is False
        assert pv.is_prerelease("6.0.11", "6.0.12") is False

    @pytest.mark.parametrize("version, release", [
        ("6.0.19", None), ("6.0.19", ""), ("6.0.19", "unknown"),
        (None, "6.0.12"), ("dev", "6.0.12"), (None, None),
    ])
    def test_false_when_either_side_is_unreadable(self, version, release):
        # Not knowing the release must not make everyone a pre-release tester.
        assert pv.is_prerelease(version, release) is False


class TestRecordSighting:
    def test_claim_hit_writes_one_statement(self, wired):
        redis, sessions = wired
        pv.record_sighting(_submission())

        assert redis.sets == [{"key": "pluginver:claim:-123456:6.0.19", "value": "1",
                               "nx": True, "ex": 21600}]
        assert len(sessions) == 1
        session = sessions[0]
        assert len(session.executed) == 1
        sql, params = session.executed[0]
        assert params == {"version": "6.0.19", "pre": 1, "h": "-123456"}
        assert "INSERT INTO player_plugin_versions" in sql
        assert "WHERE p.account_hash = :h LIMIT 1" in sql
        assert session.committed and session.closed
        assert redis.deleted == []

    def test_prerelease_is_only_written_on_first_sight(self, wired):
        _redis, sessions = wired
        pv.record_sighting(_submission())
        sql = sessions[0].executed[0][0]
        update = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        assert update.strip() == ("last_seen = NOW(), "
                                  "sightings = player_plugin_versions.sightings + 1")
        # "Ran it before it reached the Plugin Hub" stays true after release.
        assert "prerelease" not in update and "first_seen" not in update

    @pytest.mark.parametrize("version, release, pre", [
        ("6.0.12", "6.0.12", 0),
        ("6.0.11", "6.0.12", 0),
        ("6.0.19", None, 0),          # no build manifest: nobody is pre-release
        ("dev-build", "6.0.12", 0),
        ("6.0.13", "6.0.12", 1),
    ])
    def test_prerelease_flag(self, wired, monkeypatch, version, release, pre):
        _redis, sessions = wired
        monkeypatch.setattr(pv, "_release_version", lambda: release)
        pv.record_sighting(_submission(p_v=version))
        assert sessions[0].executed[0][1]["pre"] == pre

    def test_claim_miss_touches_no_database(self, wired, monkeypatch):
        redis, sessions = wired
        redis.claimed = False
        monkeypatch.setattr(pv, "_release_version",
                            lambda: pytest.fail("read the manifest without a claim"))
        pv.record_sighting(_submission())
        assert len(redis.sets) == 1
        assert sessions == [] and redis.deleted == []

    def test_database_failure_gives_the_claim_back(self, wired, monkeypatch):
        redis, sessions = wired
        monkeypatch.setattr(pv, "_private_session", lambda: sessions.append(
            FakeSession(fail=True)) or sessions[-1])
        pv.record_sighting(_submission())      # must not raise
        assert redis.deleted == ["pluginver:claim:-123456:6.0.19"]
        assert sessions[0].closed and not sessions[0].committed

    def test_after_a_failure_it_pauses_instead_of_failing_every_submission(self, wired, monkeypatch):
        redis, sessions = wired
        monkeypatch.setattr(pv, "_private_session", lambda: sessions.append(
            FakeSession(fail=True)) or sessions[-1])
        pv.record_sighting(_submission())
        pv.record_sighting(_submission(acc_hash="999"))
        pv.record_sighting(_submission(acc_hash="1000"))
        # One failed statement, then nothing: not even the Redis call.
        assert len(sessions) == 1 and len(redis.sets) == 1

        # Once the pause is over it records again.
        monkeypatch.setattr(pv, "_skip_until", 0.0)
        monkeypatch.setattr(pv, "_private_session", lambda: sessions.append(
            FakeSession()) or sessions[-1])
        pv.record_sighting(_submission(acc_hash="999"))
        assert len(sessions) == 2 and sessions[1].committed

    def test_a_failed_claim_release_is_swallowed_too(self, wired, monkeypatch):
        redis, sessions = wired
        monkeypatch.setattr(pv, "_private_session", lambda: FakeSession(fail=True))
        monkeypatch.setattr(redis, "delete", lambda key: (_ for _ in ()).throw(
            ConnectionError("redis went away")))
        pv.record_sighting(_submission())      # must not raise

    def test_opening_the_session_can_fail(self, wired, monkeypatch):
        redis, _sessions = wired

        def _no_pool():
            raise TimeoutError("QueuePool limit reached")

        monkeypatch.setattr(pv, "_private_session", _no_pool)
        pv.record_sighting(_submission())      # must not raise
        assert redis.deleted == ["pluginver:claim:-123456:6.0.19"]

    def test_mirrored_submission_records_nothing(self, wired):
        redis, sessions = wired
        with mirror_sink(336):
            pv.record_sighting(_submission())
        assert redis.sets == [] and sessions == []
        # ...and the same submission outside the sink is recorded.
        pv.record_sighting(_submission())
        assert len(redis.sets) == 1 and len(sessions) == 1

    @pytest.mark.parametrize("data", [
        {"p_v": "6.0.19"},                                   # no account
        {"acc_hash": None, "p_v": "6.0.19"},
        {"acc_hash": "", "p_v": "6.0.19"},
        {"acc_hash": "   ", "p_v": "6.0.19"},
        {"acc_hash": "-123456"},                             # no version
        {"acc_hash": "-123456", "p_v": None},
        {"acc_hash": "-123456", "p_v": ""},
        {"acc_hash": "-123456", "p_v": "  "},
        {"acc_hash": "-123456", "p_v": "unknown"},
        {"acc_hash": "-123456", "p_v": " Unknown "},
        {"acc_hash": "9" * 101, "p_v": "6.0.19"},            # longer than any stored hash
        {},
    ])
    def test_missing_fields_record_nothing(self, wired, data):
        redis, sessions = wired
        pv.record_sighting(data)
        assert redis.sets == [] and sessions == []

    @pytest.mark.parametrize("data", [None, "drop", 5, ["acc_hash"]])
    def test_not_a_submission_at_all(self, wired, data):
        redis, sessions = wired
        pv.record_sighting(data)               # must not raise
        assert redis.sets == [] and sessions == []

    def test_values_are_normalised(self, wired):
        redis, sessions = wired
        # A numeric hash straight from JSON, and a version padded past the column.
        pv.record_sighting({"acc_hash": -123456, "p_v": "  6.0.19-" + "x" * 60})
        version = sessions[0].executed[0][1]["version"]
        assert sessions[0].executed[0][1]["h"] == "-123456"
        assert len(version) == 32 and version.startswith("6.0.19-x")
        assert redis.sets[0]["key"] == f"pluginver:claim:-123456:{version}"

    def test_no_redis_records_nothing(self, wired, monkeypatch):
        _redis, sessions = wired
        monkeypatch.setattr(pv, "_redis", lambda: None)
        pv.record_sighting(_submission())
        assert sessions == []

    def test_redis_errors_are_swallowed(self, wired, monkeypatch):
        redis, sessions = wired
        redis.fail = True
        pv.record_sighting(_submission())      # must not raise
        assert sessions == []

        def _broken():
            raise ImportError("utils.redis")

        monkeypatch.setattr(pv, "_redis", _broken)
        pv.record_sighting(_submission())      # must not raise
        assert sessions == []


class TestReleaseVersionLookup:
    def test_reads_the_current_build_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TESTER_BUILDS_DIR", str(tmp_path))
        assert pv._release_version() is None    # no build published yet
        (tmp_path / "current.json").write_text(json.dumps({
            "build_id": "6.0.19-2cd73be-rl1.13.1",
            "release": {"commit": "577fa89" + "0" * 33, "version": "6.0.12"},
        }))
        assert pv._release_version() == "6.0.12"

    def test_drives_the_prerelease_flag(self, tmp_path, monkeypatch):
        sessions = []
        monkeypatch.setattr(pv, "_redis", lambda: FakeRedis())
        monkeypatch.setattr(pv, "_private_session",
                            lambda: sessions.append(FakeSession()) or sessions[-1])
        monkeypatch.setattr(pv, "_skip_until", 0.0)
        monkeypatch.setenv("TESTER_BUILDS_DIR", str(tmp_path))
        (tmp_path / "current.json").write_text(json.dumps({
            "build_id": "6.0.19-2cd73be-rl1.13.1",
            "release": {"commit": "577fa89" + "0" * 33, "version": "6.0.12"},
        }))
        pv.record_sighting(_submission(p_v="6.0.19"))
        pv.record_sighting(_submission(p_v="6.0.12", acc_hash="77"))
        assert [s.executed[0][1]["pre"] for s in sessions] == [1, 0]
