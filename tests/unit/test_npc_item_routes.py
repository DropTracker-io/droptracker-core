"""Unit tests for the public NPC/item page routes (web_api/routes/npcs.py,
items.py) — the pure helpers around the last-received registry. DB/Redis are
stubbed by tests/conftest.py, so fakes are injected explicitly."""

from datetime import datetime
from unittest.mock import MagicMock, patch

from web_api.routes import npcs


class FakeRedisHash:
    """Just enough of redis-py for the registry helpers: hset/hgetall with
    byte-encoded fields/values, like a real connection returns."""

    def __init__(self):
        self.store = {}

    def hset(self, key, mapping=None):
        self.store.setdefault(key, {}).update(
            {str(k).encode(): str(v).encode() for k, v in (mapping or {}).items()}
        )

    def hgetall(self, key):
        return dict(self.store.get(key, {}))


class TestWikiUrl:
    def test_spaces_become_underscores(self):
        assert npcs._wiki_url("Theatre of Blood") == (
            "https://oldschool.runescape.wiki/w/Theatre_of_Blood"
        )


class TestScanLastDrops:
    def _session(self, rows):
        s = MagicMock()
        s.execute.return_value.fetchall.return_value = rows
        return s

    def test_keeps_latest_drop_per_item_and_advances_cursor(self):
        rows = [
            # (drop_id, item_id, player_id, value, quantity, date_added)
            (10, 4151, 1, 100, 1, datetime(2026, 1, 1)),
            (12, 4151, 2, 100, 1, datetime(2026, 1, 2)),
            (11, 995, 3, 1, 500, datetime(2026, 1, 1)),
        ]
        latest, cursor = npcs._scan_last_drops(self._session(rows), 42, 0)
        assert cursor == 12
        assert latest[4151]["player_id"] == 2
        assert latest[4151]["drop_id"] == 12
        assert latest[995] == {
            "drop_id": 11, "player_id": 3, "ts": int(datetime(2026, 1, 1).timestamp()),
            "value": 1, "quantity": 500,
        }

    def test_skips_null_items_but_still_advances_cursor(self):
        rows = [(99, None, 1, 5, 1, datetime(2026, 1, 1))]
        latest, cursor = npcs._scan_last_drops(self._session(rows), 42, 0)
        assert latest == {}
        assert cursor == 99

    def test_empty_scan_keeps_cursor(self):
        latest, cursor = npcs._scan_last_drops(self._session([]), 42, 1234)
        assert latest == {}
        assert cursor == 1234


class TestRegistryRoundtrip:
    def test_store_then_load(self):
        fake = FakeRedisHash()
        with patch.object(npcs, "_rc", return_value=fake):
            npcs._store_last_drops(
                7,
                {4151: {"drop_id": 5, "player_id": 9, "ts": 1, "value": 2, "quantity": 1}},
                cursor=5,
            )
            latest, cursor = npcs._load_last_drops(7)
        assert cursor == 5
        assert latest == {4151: {"drop_id": 5, "player_id": 9, "ts": 1, "value": 2, "quantity": 1}}

    def test_cold_registry_loads_empty(self):
        fake = FakeRedisHash()
        with patch.object(npcs, "_rc", return_value=fake):
            latest, cursor = npcs._load_last_drops(1)
        assert latest == {}
        assert cursor is None

    def test_zero_cursor_never_stored(self):
        fake = FakeRedisHash()
        with patch.object(npcs, "_rc", return_value=fake):
            npcs._store_last_drops(7, {}, cursor=0)
        assert fake.store == {}


class TestLastDropsFor:
    def test_warm_registry_tops_up_incrementally(self):
        fake = FakeRedisHash()
        with patch.object(npcs, "_rc", return_value=fake):
            npcs._store_last_drops(
                7,
                {995: {"drop_id": 3, "player_id": 1, "ts": 1, "value": 1, "quantity": 1}},
                cursor=3,
            )
            new_rows = [(8, 4151, 2, 50, 1, datetime(2026, 2, 1))]
            s = MagicMock()
            s.execute.return_value.fetchall.return_value = new_rows
            latest, status = npcs._last_drops_for(s, 7)
        assert status == "ready"
        assert set(latest) == {995, 4151}
        # The top-up advanced the stored cursor to the newest drop.
        with patch.object(npcs, "_rc", return_value=fake):
            _, cursor = npcs._load_last_drops(7)
        assert cursor == 8

    def test_cold_and_big_defers_to_background(self):
        fake = FakeRedisHash()
        s = MagicMock()
        with patch.object(npcs, "_rc", return_value=fake), \
             patch.object(npcs, "_npc_drop_volume", return_value=npcs._INLINE_BUILD_MAX_DROPS + 1):
            latest, status = npcs._last_drops_for(s, 7)
        assert status == "building"
        assert latest == {}

    def test_cold_and_small_builds_inline(self):
        fake = FakeRedisHash()
        rows = [(2, 995, 1, 1, 1, datetime(2026, 1, 1))]
        s = MagicMock()
        s.execute.return_value.fetchall.return_value = rows
        with patch.object(npcs, "_rc", return_value=fake), \
             patch.object(npcs, "_npc_drop_volume", return_value=10):
            latest, status = npcs._last_drops_for(s, 7)
        assert status == "ready"
        assert 995 in latest
