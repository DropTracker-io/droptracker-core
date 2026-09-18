"""web_api/platform_summary.py — the homepage's month total, account count and
top bosses, against an in-memory fake Redis.

What is pinned here is the behaviour under load rather than the arithmetic: a
request must never wait on a rebuild when there is something to serve, only one
rebuild may run at a time, and a rebuild that keeps failing must not be retried
on every request.
"""

import json
from contextlib import contextmanager
from decimal import Decimal

import pytest

import web_api.platform_summary as ps

NOW = 1_800_000_000
PART = 202609


class FakeRedis:
    """Just enough of redis-py for platform_summary."""

    def __init__(self):
        self.kv = {}
        self.ttl = {}
        self.zsets = {}

    def get(self, key):
        v = self.kv.get(key)
        return v.encode() if isinstance(v, str) else v

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        self.ttl[key] = ex
        return True

    def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    def zscore(self, key, member):
        return self.zsets.get(key, {}).get(int(member))

    def zrange(self, key, start, stop, withscores=False):
        rows = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        return [(str(m).encode(), float(s)) for m, s in rows] if withscores else [m for m, _ in rows]


SNAPSHOT = {
    "at": NOW - 30,
    "member_count": 27856,
    "top_bosses": [
        {"npc_id": 14150, "name": "Yama", "loot": 31_734_859_972, "drops": 10138},
        {"npc_id": 13699, "name": "Araxxor", "loot": 22_561_921_341, "drops": 21553},
    ],
}


def _builder(result=None, calls=None, error=None):
    def build(partition):
        if calls is not None:
            calls.append(partition)
        if error is not None:
            raise error
        return dict(result if result is not None else SNAPSHOT, at=NOW)

    return build


@pytest.fixture
def r():
    return FakeRedis()


# ── Month total ───────────────────────────────────────────────────────────────

def test_month_total_is_the_figure_the_global_lootboard_published(r):
    r.zsets[f"gleaderboard:{PART}"] = {2: 287_816_175_687.0, 14: 9_000_000.0}
    # The player board says something slightly different (it is a couple of
    # minutes ahead of the board). The published figure wins, so the homepage,
    # the lootboard image and the Discord counter all agree.
    r.zsets[f"leaderboard:{PART}"] = {101: 200_000_000_000.0, 102: 87_858_001_520.0}

    assert ps.month_total(r, PART) == 287_816_175_687


def test_month_total_sums_the_player_board_until_the_first_board_is_drawn(r):
    r.zsets[f"leaderboard:{PART}"] = {101: 1_500_000.0, 102: 250_000.0}

    assert ps.month_total(r, PART) == 1_750_000


def test_month_total_is_unknown_rather_than_zero(r):
    # No board and no players yet: "nobody can say" is not the same as 0 gp, and
    # the homepage renders the two differently.
    assert ps.month_total(r, PART) is None
    assert ps.month_total(None, PART) is None


# ── Read path ─────────────────────────────────────────────────────────────────

def test_a_fresh_snapshot_is_served_without_touching_the_database(r):
    r.zsets[f"gleaderboard:{PART}"] = {2: 5_000_000_000.0}
    r.kv[ps.SNAPSHOT_KEY.format(partition=PART)] = json.dumps(SNAPSHOT)
    calls = []

    payload, stale = ps.load(conn=r, now=NOW, partition=PART, build=_builder(calls=calls))

    assert stale is False and calls == []
    assert payload["monthly_loot"] == {"value": 5_000_000_000, "value_formatted": "5.00B"}
    assert payload["member_count"] == 27856
    assert payload["snapshot_at"] == NOW - 30
    assert [b["name"] for b in payload["top_bosses"]] == ["Yama", "Araxxor"]
    assert payload["top_bosses"][0]["loot"] == {
        "value": 31_734_859_972, "value_formatted": "31.73B",
    }
    assert payload["top_bosses"][0]["drops"] == 10138


def test_a_stale_snapshot_is_still_served_and_only_flagged(r):
    old = dict(SNAPSHOT, at=NOW - ps.FRESH_FOR - 1)
    r.kv[ps.SNAPSHOT_KEY.format(partition=PART)] = json.dumps(old)
    calls = []

    payload, stale = ps.load(conn=r, now=NOW, partition=PART, build=_builder(calls=calls))

    # The visitor gets the old boss order now; the rebuild is the caller's to
    # schedule behind the response.
    assert stale is True and calls == []
    assert payload["member_count"] == 27856
    assert len(payload["top_bosses"]) == 2


def test_nothing_cached_builds_once_in_front_of_the_response(r):
    calls = []

    payload, stale = ps.load(conn=r, now=NOW, partition=PART, build=_builder(calls=calls))

    assert calls == [PART] and stale is False
    assert payload["member_count"] == 27856
    key = ps.SNAPSHOT_KEY.format(partition=PART)
    assert json.loads(r.kv[key])["member_count"] == 27856
    # Kept far longer than it stays fresh: a restart must not start cold.
    assert r.ttl[key] == ps.KEEP_FOR > ps.FRESH_FOR
    assert ps.LOCK_KEY.format(partition=PART) not in r.kv


def test_a_second_cold_request_does_not_build_again(r):
    r.kv[ps.LOCK_KEY.format(partition=PART)] = "somebody else is building"
    r.zsets[f"gleaderboard:{PART}"] = {2: 1_000.0}
    calls = []

    payload, stale = ps.load(conn=r, now=NOW, partition=PART, build=_builder(calls=calls))

    # It answers with what is free and leaves the slow fields empty, which the
    # homepage renders as "being tallied".
    assert calls == [] and stale is False
    assert payload["monthly_loot"]["value"] == 1_000
    assert payload["member_count"] is None
    assert payload["top_bosses"] == [] and payload["snapshot_at"] is None


def test_each_month_has_its_own_snapshot(r):
    r.kv[ps.SNAPSHOT_KEY.format(partition=PART)] = json.dumps(SNAPSHOT)
    calls = []

    ps.load(conn=r, now=NOW, partition=PART + 1, build=_builder(calls=calls))

    # September's bosses are not October's.
    assert calls == [PART + 1]


def test_redis_down_answers_empty_instead_of_querying_per_request():
    calls = []

    payload, stale = ps.load(conn=None, now=NOW, partition=PART, build=_builder(calls=calls))

    # With nowhere to keep the result, building would mean a one-second query on
    # every request. Better to say nothing.
    assert calls == [] and stale is False
    assert payload["monthly_loot"] is None and payload["member_count"] is None
    assert payload["top_bosses"] == []


@pytest.mark.parametrize("junk", [
    "not json",
    json.dumps([1, 2, 3]),
    json.dumps({"member_count": 5}),                      # no timestamp
    json.dumps({"at": NOW, "top_bosses": "Yama"}),        # wrong shape
])
def test_an_unreadable_snapshot_is_rebuilt_not_rendered(r, junk):
    r.kv[ps.SNAPSHOT_KEY.format(partition=PART)] = junk
    calls = []

    payload, _ = ps.load(conn=r, now=NOW, partition=PART, build=_builder(calls=calls))

    assert calls == [PART]
    assert payload["member_count"] == 27856


# ── Rebuild ───────────────────────────────────────────────────────────────────

def test_refresh_is_single_flight(r):
    r.kv[ps.LOCK_KEY.format(partition=PART)] = "held"
    calls = []

    assert ps.refresh(PART, conn=r, build=_builder(calls=calls)) is None
    assert calls == []


def test_a_failed_rebuild_keeps_the_old_snapshot_and_backs_off(r):
    key = ps.SNAPSHOT_KEY.format(partition=PART)
    r.kv[key] = json.dumps(SNAPSHOT)
    calls = []
    failing = _builder(calls=calls, error=RuntimeError("statement timeout"))

    assert ps.refresh(PART, conn=r, build=failing) is None
    assert json.loads(r.kv[key]) == SNAPSHOT

    # The lock is left to expire, so the next request does not retry at once…
    lock = ps.LOCK_KEY.format(partition=PART)
    assert r.ttl[lock] == ps.LOCK_TTL
    assert ps.refresh(PART, conn=r, build=failing) is None
    assert calls == [PART]

    # …and once it has, a working build goes through and clears it.
    del r.kv[lock]
    assert ps.refresh(PART, conn=r, build=_builder())["member_count"] == 27856
    assert lock not in r.kv


def test_build_snapshot_shapes_database_rows(monkeypatch):
    class Result:
        def __init__(self, scalar=None, rows=()):
            self._scalar, self._rows = scalar, rows

        def scalar(self):
            return self._scalar

        def fetchall(self):
            return list(self._rows)

    class Session:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))
            if "COUNT(" in str(statement):
                return Result(scalar=27856)
            # MariaDB hands SUM() back as Decimal.
            return Result(rows=[(14150, "Yama", Decimal("31734859972"), Decimal("10138"))])

    session = Session()

    @contextmanager
    def fake_db_session():
        yield session

    import web_api.common as common

    monkeypatch.setattr(common, "db_session", fake_db_session)

    snapshot = ps.build_snapshot(PART)

    assert snapshot["member_count"] == 27856
    assert snapshot["top_bosses"] == [
        {"npc_id": 14150, "name": "Yama", "loot": 31_734_859_972, "drops": 10138},
    ]
    json.dumps(snapshot)  # must survive the trip into Redis (no Decimal left)

    # "Accounts" means players. The association table also carries user-only
    # rows and the odd duplicate, so a bare row count overstated it by ~1,100
    # against production data (27,864 rows, 26,717 accounts).
    member_sql, member_params = session.statements[0]
    assert member_params == {"gid": ps.GLOBAL_GROUP_ID}
    assert "COUNT(DISTINCT uga.player_id)" in member_sql
    assert "JOIN players" in member_sql

    boss_sql, boss_params = session.statements[1]
    assert boss_params == {"partition": PART, "lim": ps.TOP_BOSSES}
    # The whole point: "members of the global group" is everybody, so the
    # 27k-row membership join that made this a 20-second query is gone.
    assert "user_group_association" not in boss_sql
    assert "max_statement_time" in boss_sql
