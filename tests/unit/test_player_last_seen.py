"""utils/player_last_seen.py: when each player's plugin was last seen.

It sits on the submission path of both transports and on the notifications
poll, so the same properties as utils/plugin_versions.py matter: a claimed
account costs one Redis call, an unclaimed one exactly one statement in its
own session, nothing raises into the caller, a failed write gives the claim
back, and neither manual website submissions nor mirrored traffic count.
"""
from __future__ import annotations

import pytest

from utils import player_last_seen as pls
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
        self.sets.append({"key": key, "nx": nx, "ex": ex})
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
            raise RuntimeError("Table 'data.player_last_seen' doesn't exist")

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture()
def wired(monkeypatch):
    redis = FakeRedis()
    sessions = []

    def _session():
        sessions.append(FakeSession())
        return sessions[-1]

    monkeypatch.setattr(pls, "_redis", lambda: redis)
    monkeypatch.setattr(pls, "_private_session", _session)
    monkeypatch.setattr(pls, "_skip_until", 0.0)
    return redis, sessions


def _submission(**over):
    data = {"acc_hash": "-123456", "p_v": "6.0.19", "player": "Tester", "type": "drop"}
    data.update(over)
    return data


class TestRecordSubmission:
    def test_a_claim_writes_one_statement_by_account_hash(self, wired):
        redis, sessions = wired
        pls.record_submission(_submission())
        assert redis.sets == [{"key": "lastseen:claim:h:-123456", "nx": True,
                               "ex": pls.CLAIM_TTL_SECONDS}]
        assert len(sessions) == 1
        (sql, params), = sessions[0].executed
        assert "INSERT INTO player_last_seen" in sql
        assert "GREATEST" in sql  # never moves anyone backwards
        assert params == {"h": "-123456"}
        assert sessions[0].committed and sessions[0].closed

    def test_inside_the_window_costs_no_database_work(self, wired):
        redis, sessions = wired
        redis.claimed = False
        pls.record_submission(_submission())
        assert sessions == []

    def test_any_submission_type_counts_even_without_a_version(self, wired):
        # Being seen is about the account, not the version: unlike the
        # tester sightings, a missing p_v is still the plugin talking to us.
        _, sessions = wired
        pls.record_submission(_submission(type="config_snapshot", p_v=None))
        assert len(sessions) == 1

    def test_a_manual_website_submission_is_not_the_plugin(self, wired):
        redis, sessions = wired
        pls.record_submission(_submission(intake_source="manual"))
        assert redis.sets == [] and sessions == []

    @pytest.mark.parametrize("acc_hash", [None, "", "   ", "x" * 101])
    def test_no_usable_account_is_skipped(self, wired, acc_hash):
        redis, sessions = wired
        pls.record_submission(_submission(acc_hash=acc_hash))
        assert redis.sets == [] and sessions == []

    def test_mirrored_traffic_is_not_recorded(self, wired):
        redis, sessions = wired
        with mirror_sink(336):
            pls.record_submission(_submission())
        assert redis.sets == [] and sessions == []

    def test_a_failed_write_gives_the_claim_back_and_pauses(self, wired, monkeypatch):
        redis, _ = wired
        failing = []

        def _session():
            failing.append(FakeSession(fail=True))
            return failing[-1]

        monkeypatch.setattr(pls, "_private_session", _session)
        pls.record_submission(_submission())  # must not raise
        assert redis.deleted == ["lastseen:claim:h:-123456"]
        assert failing[0].closed
        pls.record_submission(_submission(acc_hash="-999"))
        assert len(failing) == 1  # backing off, no second statement

    def test_redis_down_never_raises(self, wired):
        redis, sessions = wired
        redis.fail = True
        pls.record_submission(_submission())
        assert sessions == []

    def test_junk_input_never_raises(self, wired):
        pls.record_submission(None)
        pls.record_submission("not a dict")


class TestRecordPlayer:
    def test_a_poll_writes_by_player_id(self, wired):
        redis, sessions = wired
        pls.record_player(42)
        assert redis.sets[0]["key"] == "lastseen:claim:p:42"
        (sql, params), = sessions[0].executed
        assert params == {"pid": 42}

    def test_player_zero_is_a_real_account(self, wired):
        _, sessions = wired
        pls.record_player(0)
        assert sessions[0].executed[0][1] == {"pid": 0}

    def test_none_is_skipped(self, wired):
        redis, sessions = wired
        pls.record_player(None)
        assert redis.sets == [] and sessions == []
