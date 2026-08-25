"""Unit tests for the public NPC/item page routes (web_api/routes/npcs.py,
items.py) — the pure helpers around the last-received registry. DB/Redis are
stubbed by tests/conftest.py, so fakes are injected explicitly."""

from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import web_api.routes.events as evr
from web_api import common as web_common
from web_api.routes import items, npc_source_aliases, npcs

from tests.unit.test_event_auth_modes import _S, _SessionCM


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


class TestVariantItemIds:
    """One item name spans several ids; readers of per-item-id tables need all
    of them (see items.variant_item_ids)."""

    def _session(self, ids):
        s = MagicMock()
        s.query.return_value.filter.return_value.all.return_value = [(i,) for i in ids]
        return s

    def test_returns_every_id_sharing_the_name(self):
        s = self._session([22405, 22446, 22447, 22448])
        assert items.variant_item_ids(s, "Vial of blood", 22405) == [
            22405, 22446, 22447, 22448
        ]

    def test_unknown_name_falls_back_to_the_requested_id(self):
        assert items.variant_item_ids(self._session([]), "Nonexistent", 7) == [7]

    def test_blank_name_never_queries(self):
        s = MagicMock()
        assert items.variant_item_ids(s, None, 7) == [7]
        s.query.assert_not_called()


class TestSources:
    """items._sources: the drop-source list behind the item page and the event
    task form's per-item "restrict to specific NPC sources" picker."""

    @staticmethod
    @contextmanager
    def _db(*, wiki_rows, observed_rows=(), total=None):
        """Patch db_session so _sources sees canned wiki + observed results.

        Call order inside _sources: total (fetchone) -> wiki rows (fetchall)
        -> _observed_source_rows' drop scan (fetchall) -> its npc-name lookup
        (fetchall).
        """
        s = MagicMock()
        names = {r[1] for r in wiki_rows} | {r[1] for r in observed_rows}
        s.execute.side_effect = [
            MagicMock(fetchone=lambda: (total if total is not None else len(names),)),
            MagicMock(fetchall=lambda: list(wiki_rows)),
            # (npc_id, player_id) drop rows: enough of each to clear the
            # min-drops / min-players thresholds.
            MagicMock(fetchall=lambda: [
                (r[0], p) for r in observed_rows for p in range(1, 6)
            ]),
            MagicMock(fetchall=lambda: [(r[0], r[1]) for r in observed_rows]),
        ]

        @contextmanager
        def fake_session():
            yield s

        # _sources memoizes per id-set in the shared in-process cache.
        web_common._cache.clear()
        with patch.object(items, "db_session", fake_session):
            yield s

    def test_unions_sources_across_every_variant_id(self):
        """The regression this guards: resolving "Vial of blood" to one id
        (MIN -> 22405) asked the wiki table about an id holding a single ToB
        Hard Mode row while every receipt sits on 22446, so the picker offered
        one of three real sources."""
        wiki = [(13961, "Theatre of Blood: Hard Mode", "1", 0.066, 1, 1)]
        observed = [(13699, "Theatre of Blood"), (13958, "Theatre of Blood: Entry Mode")]
        with self._db(wiki_rows=wiki, observed_rows=observed) as s:
            out = items._sources([22405, 22446, 22447, 22448])
        assert [n["name"] for n in out["npcs"]] == [
            "Theatre of Blood: Hard Mode",
            "Theatre of Blood",
            "Theatre of Blood: Entry Mode",
        ]
        # Every variant id reaches both source queries.
        for call in s.execute.call_args_list[:3]:
            assert call.args[1]["ids"] == [22405, 22446, 22447, 22448]

    def test_observed_scan_skipped_when_wiki_already_fills_the_cap(self):
        """The scan random-reads up to 50k drop rows (~10s for a ubiquitous
        item) and its extras are discarded once the list is full."""
        wiki = [
            (i, f"NPC {i}", "1", 0.001, 1, 1) for i in range(items._SOURCES_LIMIT)
        ]
        with self._db(wiki_rows=wiki, observed_rows=[(999, "Never reached")]) as s:
            out = items._sources([1619, 1620])
        assert len(out["npcs"]) == items._SOURCES_LIMIT
        assert "Never reached" not in {n["name"] for n in out["npcs"]}
        # total + wiki only — the drop scan never ran.
        assert s.execute.call_count == 2

    def test_sources_query_covers_the_most_sourced_wiki_item(self):
        """Regression: the source list was capped at the 100 rarest wiki rows,
        so gem-drop-table sources vanished — Kree'arra drops Uncut diamond at
        1/2,501 but ranked ~246 of 453 sources, past a cutoff of 1/8,192. The
        DB must be asked for enough rows to cover the wiki's most-sourced item
        ("Coins": 601 distinct sources)."""
        wiki = [(3162, "Kree'arra", "1", 0.0004, 1, 1)]
        with self._db(wiki_rows=wiki) as s:
            items._sources([1617])
        lim = s.execute.call_args_list[1].args[1]["lim"]
        assert lim > 601

    def test_observed_scan_skipped_once_wiki_knows_plenty_of_sources(self):
        """The drop scan exists to fill wiki GAPS. An item the wiki already
        credits with 100+ sources has no gap worth a ~10s 50k-row scan, even
        when those rows no longer fill the (now larger) source cap."""
        wiki = [(i, f"NPC {i}", "1", 0.001, 1, 1) for i in range(100)]
        with self._db(wiki_rows=wiki, observed_rows=[(999, "Never reached")]) as s:
            out = items._sources([1621, 1622])
        assert "Never reached" not in {n["name"] for n in out["npcs"]}
        # total + wiki only — the drop scan never ran.
        assert s.execute.call_count == 2

    def test_alias_members_survive_the_union(self):
        """A merged display alias must still carry the real recorded names the
        task engine matches drops by."""
        wiki = [(13974, "Reward cart (Wintertodt)", "1", 0.01, 1, 1),
                (20693, "Supply crate (Wintertodt)", "1", 0.01, 1, 1)]
        with self._db(wiki_rows=wiki):
            out = items._sources([20718])
        assert [n["name"] for n in out["npcs"]] == ["Wintertodt"]
        assert out["npcs"][0]["members"] == [
            "Reward cart (Wintertodt)", "Supply crate (Wintertodt)"
        ]

    def test_alias_carries_member_ids_for_id_keyed_callers(self):
        """The points include/exclude lists store an npc id, not a name, so an
        alias has to expose the ids of the rows it merged. The representative
        id (13974, chosen for the icon) is not enough on its own — selecting
        "Wintertodt" there must blacklist the supply crate too."""
        wiki = [(13974, "Reward cart (Wintertodt)", "1", 0.01, 1, 1),
                (20693, "Supply crate (Wintertodt)", "1", 0.01, 1, 1)]
        with self._db(wiki_rows=wiki):
            out = items._sources([20718])
        assert out["npcs"][0]["member_ids"] == [13974, 20693]

    def test_result_is_cached_per_id_set(self):
        wiki = [(963, "Kalphite Queen", "1", 0.0078, 1, 1)]
        with self._db(wiki_rows=wiki) as s:
            first = items._sources([2513, 3140])
            queries = s.execute.call_count
            again = items._sources([3140, 2513])  # same set, either order
        assert first == again
        assert s.execute.call_count == queries  # second call served from cache


class TestAliasSearchEntries:
    """npc_source_aliases.alias_search_entries: the synthetic autocomplete rows
    prepended to /events/meta/npcs. They must carry the same fields as a real
    NPC row so the picker renders an alias identically to a monster it added."""

    def test_matching_alias_carries_icon_and_tracked(self):
        [entry] = npc_source_aliases.alias_search_entries("winter")
        assert entry == {
            "id": 13974,
            "name": "Wintertodt",
            "icon_url": "https://www.droptracker.io/img/npcdb/13974.png",
            # An alias only exists for an activity whose loot is recorded under
            # its member NPCs, so it is always a real tracked source.
            "tracked": True,
        }

    def test_non_matching_query_is_empty(self):
        assert npc_source_aliases.alias_search_entries("graardor") == []

    def test_below_min_length_is_empty(self):
        assert npc_source_aliases.alias_search_entries("w") == []


class TestSearchNpcsEndpoint:
    """GET /events/meta/npcs — NPC autocomplete for the task-form source picker.
    Each result carries icon_url + a `tracked` flag (any form id with drop
    history) so a monster added via search matches the source-picker rows."""

    @pytest.fixture()
    def client(self):
        import web_api

        return web_api.create_app().test_client()

    def _wire(self, monkeypatch, session):
        monkeypatch.setattr(evr, "current_user_id", lambda: 1)
        monkeypatch.setattr(evr, "db_session", lambda: _SessionCM(session))

    async def test_results_are_enriched_with_icon_and_tracked(self, client, monkeypatch):
        # Batch 1: deduped (min id, name) rows. Batch 2: the names that have
        # any tracked form id — only Kree'arra here.
        s = _S(
            [(3162, "Kree'arra"), (2215, "General Graardor")],
            [("Kree'arra",)],
        )
        self._wire(monkeypatch, s)
        r = await client.get("/api/v1/events/meta/npcs?q=arra")
        assert r.status_code == 200
        assert (await r.get_json()) == [
            {
                "id": 3162,
                "name": "Kree'arra",
                "icon_url": "https://www.droptracker.io/img/npcdb/3162.png",
                "tracked": True,
            },
            {
                "id": 2215,
                "name": "General Graardor",
                "icon_url": "https://www.droptracker.io/img/npcdb/2215.png",
                "tracked": False,
            },
        ]

    async def test_no_matches_skips_the_tracked_probe(self, client, monkeypatch):
        # Only one scripted batch: an empty name list must NOT issue the
        # tracked-names query (the scripted session asserts on an extra query).
        s = _S([])
        self._wire(monkeypatch, s)
        r = await client.get("/api/v1/events/meta/npcs?q=zzzzz")
        assert r.status_code == 200
        assert (await r.get_json()) == []

    async def test_short_query_returns_empty_without_touching_db(self, client, monkeypatch):
        # < 2 chars short-circuits before any query (zero scripted batches).
        s = _S()
        self._wire(monkeypatch, s)
        r = await client.get("/api/v1/events/meta/npcs?q=a")
        assert r.status_code == 200
        assert (await r.get_json()) == []
