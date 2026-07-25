"""Unit tests for the loot tracker's all-time mode (`GET /players/{id}/loot?partition=all`).

All-time is the fold of every month the account has, never one unbounded
lifetime scan: each read stays a bounded ``date_added`` seek, months are cached
in Redis, and a MariaDB statement timeout becomes a clean 503. DB/Redis are
stubbed by tests/conftest.py, so the session here is a fake that records SQL.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from web_api.routes import profiles


CURRENT = 202607


# --------------------------------------------------------------------------- #
# Pure param parsing
# --------------------------------------------------------------------------- #
class TestParseLootPartition:
    def test_blank_defaults_to_current_month(self):
        assert profiles._parse_loot_partition("", CURRENT) == (CURRENT, False, None)

    def test_month_passes_through(self):
        assert profiles._parse_loot_partition("202411", CURRENT) == (202411, False, None)

    def test_all_selects_all_time_and_keeps_current_as_newest_month(self):
        partition, all_time, err = profiles._parse_loot_partition("all", CURRENT)
        assert (partition, all_time, err) == (CURRENT, True, None)

    @pytest.mark.parametrize("raw", ["ALL", " all ", "All"])
    def test_all_is_case_and_space_insensitive(self, raw):
        assert profiles._parse_loot_partition(raw, CURRENT)[1] is True

    def test_non_numeric_is_rejected_and_mentions_all(self):
        partition, all_time, err = profiles._parse_loot_partition("lifetime", CURRENT)
        assert partition is None and all_time is False
        assert "all" in err

    def test_future_month_is_rejected(self):
        assert profiles._parse_loot_partition(str(CURRENT + 1), CURRENT)[2] is not None

    def test_out_of_range_month_is_rejected(self):
        assert profiles._parse_loot_partition("202613", CURRENT)[2] is not None
        assert profiles._parse_loot_partition("201912", CURRENT)[2] is not None


# --------------------------------------------------------------------------- #
# Month enumeration + folding (the whole all-time build, minus SQL)
# --------------------------------------------------------------------------- #
class TestMonthsBetween:
    def test_inclusive_of_both_ends(self):
        assert profiles._months_between(202605, 202607) == [202605, 202606, 202607]

    def test_rolls_over_the_year(self):
        assert profiles._months_between(202511, 202602) == [202511, 202512, 202601, 202602]

    def test_single_month(self):
        assert profiles._months_between(202607, 202607) == [202607]

    def test_earliest_after_current_is_empty(self):
        assert profiles._months_between(202608, 202607) == []

    def test_a_junk_earliest_row_cannot_walk_back_decades(self):
        months = profiles._months_between(197001, 202607)
        assert months[0] == profiles._EARLIEST_SUPPORTED_PARTITION


def _box(npc_id=8061, name="Vorkath", kills=10, items=()):
    total = sum(i["loot"]["value"] for i in items)
    return {"npc_id": npc_id, "name": name, "kills": kills, "loot": profiles.money(total), "items": list(items)}


def _item(item_id=22006, name="Vorkath's head", qty=1, value=1_000, drops=1, first_ts=None, last_ts=None):
    out = {"item_id": item_id, "name": name, "quantity": qty, "loot": profiles.money(value), "drops": drops}
    if first_ts is not None:
        out["first_ts"] = first_ts
    if last_ts is not None:
        out["last_ts"] = last_ts
    return out


class TestFoldNpcBoxes:
    def test_sums_kills_quantities_values_and_drops(self):
        folded = profiles._fold_npc_boxes([
            [_box(kills=3, items=[_item(qty=2, value=500, drops=2)])],
            [_box(kills=4, items=[_item(qty=5, value=1_500, drops=3)])],
        ])
        npc = folded[0]
        assert npc["kills"] == 7
        assert npc["loot"]["value"] == 2_000
        assert npc["items"][0]["quantity"] == 7
        assert npc["items"][0]["drops"] == 5
        assert npc["items"][0]["loot"]["value"] == 2_000

    def test_keeps_the_earliest_first_seen_and_latest_last_seen(self):
        folded = profiles._fold_npc_boxes([
            [_box(items=[_item(first_ts=100, last_ts=200)])],
            [_box(items=[_item(first_ts=50, last_ts=900)])],
        ])
        assert folded[0]["items"][0]["first_ts"] == 50
        assert folded[0]["items"][0]["last_ts"] == 900

    def test_timestamps_are_omitted_when_no_month_had_them(self):
        folded = profiles._fold_npc_boxes([[_box(items=[_item()])]])
        assert "first_ts" not in folded[0]["items"][0]
        assert "last_ts" not in folded[0]["items"][0]

    def test_npcs_and_items_come_back_sorted_by_value(self):
        folded = profiles._fold_npc_boxes([
            [
                _box(npc_id=1, name="Small", items=[_item(item_id=1, value=10)]),
                _box(npc_id=2, name="Big", items=[
                    _item(item_id=2, value=100),
                    _item(item_id=3, value=900),
                ]),
            ],
        ])
        assert [n["name"] for n in folded] == ["Big", "Small"]
        assert [i["item_id"] for i in folded[0]["items"]] == [3, 2]

    def test_no_months_is_no_npcs(self):
        assert profiles._fold_npc_boxes([]) == []


# --------------------------------------------------------------------------- #
# Month cache (Redis; a miss must only ever cost a re-read)
# --------------------------------------------------------------------------- #
class TestMonthCache:
    class _FakeRedis:
        def __init__(self):
            self.store = {}
            self.ttls = {}

        def get(self, key):
            return self.store.get(key)

        def setex(self, key, ttl, value):
            self.store[key] = value
            self.ttls[key] = ttl

    def test_round_trips_compressed(self, monkeypatch):
        conn = self._FakeRedis()
        monkeypatch.setattr(profiles, "_rc", lambda: conn)
        boxes = [_box(items=[_item(first_ts=1, last_ts=2)])]

        profiles._month_cache_set("k", boxes, 60)
        assert conn.ttls["k"] == 60
        assert isinstance(conn.store["k"], bytes)
        assert profiles._month_cache_get("k") == boxes

    def test_miss_is_none(self, monkeypatch):
        monkeypatch.setattr(profiles, "_rc", lambda: self._FakeRedis())
        assert profiles._month_cache_get("nope") is None

    def test_no_redis_fails_open(self, monkeypatch):
        monkeypatch.setattr(profiles, "_rc", lambda: None)
        assert profiles._month_cache_get("k") is None
        profiles._month_cache_set("k", [], 60)  # must not raise

    def test_corrupt_entry_fails_open(self, monkeypatch):
        conn = self._FakeRedis()
        conn.store["k"] = b"not-zlib"
        monkeypatch.setattr(profiles, "_rc", lambda: conn)
        assert profiles._month_cache_get("k") is None


# --------------------------------------------------------------------------- #
# Timeout classification
# --------------------------------------------------------------------------- #
class TestIsTimeoutError:
    def _err(self, code):
        err = OperationalError("SELECT 1", {}, Exception())
        err.orig = MagicMock()
        err.orig.args = (code, "boom")
        return err

    @pytest.mark.parametrize("code", [1969, 3024, 2013])
    def test_mariadb_timeout_codes(self, code):
        assert profiles._is_timeout_error(self._err(code)) is True

    def test_other_operational_errors_are_not_timeouts(self):
        assert profiles._is_timeout_error(self._err(1146)) is False

    def test_missing_orig_args_is_not_a_timeout(self):
        err = OperationalError("SELECT 1", {}, Exception())
        err.orig = None
        assert profiles._is_timeout_error(err) is False


# --------------------------------------------------------------------------- #
# Route behaviour
# --------------------------------------------------------------------------- #
class _FakeSession:
    """Records every statement executed; serves scripted rows for the two
    aggregate reads and the earliest-partition lookup, in the order the route
    issues them."""

    def __init__(self, item_rows=None, kill_rows=None, earliest=202410, raise_on_scan=None):
        self.sql = []
        self.item_rows = item_rows or []
        self.kill_rows = kill_rows or []
        self.earliest = earliest
        self.raise_on_scan = raise_on_scan

    # `s.query(Player).filter(...).first()`
    def query(self, *a, **k):
        player = MagicMock()
        player.player_id = 42
        player.hidden = False
        player.user = None
        return MagicMock(filter=lambda *a, **k: MagicMock(first=lambda: player))

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if sql.startswith("SET SESSION"):
            return MagicMock()
        if "SELECT `partition` FROM drops" in sql:
            return MagicMock(first=lambda: (self.earliest,))
        if self.raise_on_scan is not None:
            raise self.raise_on_scan
        rows = self.kill_rows if "COUNT(DISTINCT" in sql else self.item_rows
        return MagicMock(fetchall=lambda: rows)


class _SessionCM:
    def __init__(self, s):
        self.s = s

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, session):
    monkeypatch.setattr(profiles, "db_session", lambda: _SessionCM(session))
    # The in-process payload cache and the Redis month cache are shared state;
    # both are exercised on their own below.
    monkeypatch.setattr(profiles, "cache_get", lambda *a, **k: None)
    monkeypatch.setattr(profiles, "cache_set", lambda *a, **k: None)
    monkeypatch.setattr(profiles, "_month_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(profiles, "_month_cache_set", lambda *a, **k: None)


def _drop_row(npc_id=8061, npc="Vorkath", item_id=22006, item="Vorkath's head", qty=4, loot=120_000_000):
    when = datetime(2026, 7, 1, 12, 0, 0)
    return (npc_id, npc, item_id, item, qty, loot, 4, when, when)


def _scan_sql(session):
    """Just the two drops aggregates (not SET SESSION / earliest-partition)."""
    return [s for s in session.sql if "FROM drops d" in s]


class TestPlayerLootAllTime:
    async def test_month_view_reads_one_bounded_month(self, client, monkeypatch):
        s = _FakeSession(item_rows=[_drop_row()], kill_rows=[(8061, 214)])
        _wire(monkeypatch, s)

        r = await client.get("/api/v1/players/42/loot?partition=202411")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["partition"] == 202411
        assert body["all_time"] is False
        assert body["npcs"][0]["kills"] == 214

        scans = _scan_sql(s)
        assert len(scans) == 2
        for sql in scans:
            assert "d.date_added >= :start" in sql
            assert "ix_drops_player_id_date_added" in sql
        # No statement cap on the cheap bounded read.
        assert not [x for x in s.sql if "max_statement_time" in x]

    async def test_all_time_folds_every_month_and_never_scans_unbounded(self, client, monkeypatch):
        s = _FakeSession(item_rows=[_drop_row()], kill_rows=[(8061, 3)], earliest=202605)
        _wire(monkeypatch, s)
        monkeypatch.setattr(profiles, "period_to_partition", lambda *_a: 202607)

        r = await client.get("/api/v1/players/42/loot?partition=all")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["all_time"] is True
        assert body["earliest_partition"] == 202605
        # 202605, 202606, 202607 each contribute their month's rows.
        assert body["npcs"][0]["kills"] == 9
        assert body["npcs"][0]["items"][0]["quantity"] == 12

        scans = _scan_sql(s)
        assert len(scans) == 6  # two statements per month, all range-bounded
        for sql in scans:
            assert "d.date_added >= :start" in sql
        # The kill count still ignores drops with no NPC attached.
        assert "d.npc_id IS NOT NULL" in [x for x in scans if "COUNT(DISTINCT" in x][0]

    async def test_all_time_caps_execution_and_always_resets_it(self, client, monkeypatch):
        s = _FakeSession(item_rows=[_drop_row()], kill_rows=[(8061, 900)])
        _wire(monkeypatch, s)

        await client.get("/api/v1/players/42/loot?partition=all")

        caps = [x for x in s.sql if "max_statement_time" in x]
        assert caps[0] == f"SET SESSION max_statement_time = {profiles._STATEMENT_TIMEOUT_SECONDS}"
        assert caps[-1] == "SET SESSION max_statement_time = 0"

    async def test_all_time_timeout_becomes_503(self, client, monkeypatch):
        err = OperationalError("SELECT", {}, Exception())
        err.orig = MagicMock()
        err.orig.args = (1969, "Query execution was interrupted (max_statement_time exceeded)")
        s = _FakeSession(raise_on_scan=err)
        _wire(monkeypatch, s)

        r = await client.get("/api/v1/players/42/loot?partition=all")
        assert r.status_code == 503
        assert "month" in (await r.get_json())["detail"]
        # The cap is still reset even though the query died under it.
        assert s.sql[-1] == "SET SESSION max_statement_time = 0"

    async def test_bad_partition_is_400(self, client, monkeypatch):
        s = _FakeSession()
        _wire(monkeypatch, s)

        r = await client.get("/api/v1/players/42/loot?partition=lifetime")
        assert r.status_code == 400
        assert not s.sql
