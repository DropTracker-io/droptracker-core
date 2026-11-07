"""``/v2/npcs`` and ``npc_name`` on personal bests (suggestion #232).

Integrations kept a hand-made npc_id -> name table to label PBs and edited it
on every new boss. The PB entries now carry the name, and the catalogue (with
the wiki drop table per NPC) is served as reference data.
"""
from __future__ import annotations

import types

import pytest

import data_api.routes.npcs as npcs
from data_api import sections as sect

_ROWS = [
    (2042, "Zulrah"),
    (13699, "Theatre of Blood"),
    (13961, "Theatre of Blood: Hard Mode"),
    (13958, "Theatre of Blood: Entry Mode"),
    (14708, "Doom of Mokhaiotl (Level: 1)"),
    (14709, "Doom of Mokhaiotl (Level: 2)"),
    (900006, "The Fractured Archive"),
]


class TestFilter:
    def test_name_goes_through_the_resolver_and_q_is_a_substring(self):
        resolve = lambda raw: [2042] if raw == "zulrah" else []
        assert npcs.filter_npcs(_ROWS, "zulrah", None, resolve) == [(2042, "Zulrah")]
        assert [r[0] for r in npcs.filter_npcs(_ROWS, None, "DOOM", resolve)] == [14708, 14709]
        assert npcs.filter_npcs(_ROWS, "zulrah", "doom", resolve) == []

    def test_unknown_name_is_an_empty_list_not_an_error(self):
        assert npcs.filter_npcs(_ROWS, "nobody", None, lambda raw: []) == []


class TestRow:
    def test_synthetic_ids_are_flagged(self, monkeypatch):
        monkeypatch.setattr(sect, "_boss_metric_of", lambda name: None)
        npcs._wom_metric_cache.clear()
        assert npcs.npc_row(2042, "Zulrah")["synthetic"] is False
        assert npcs.npc_row(900006, "The Fractured Archive")["synthetic"] is True

    def test_a_wom_import_failure_does_not_cost_the_row(self, monkeypatch):
        def boom(_name):
            raise ImportError("cycle")

        monkeypatch.setattr(sect, "_boss_metric_of", boom)
        npcs._wom_metric_cache.clear()
        assert npcs.npc_row(2042, "Zulrah")["wom_metric"] is None


class TestDropTableItems:
    def test_repeated_lines_collapse_and_distinct_lines_stay(self):
        rows = [
            (12922, "Tanzanite fang", "1", 0, 0.00195312, 2),
            (12922, "Tanzanite fang", "1", 0, 0.00195312, 2),   # import duplicate
            (12934, "Zulrah's scales", "100-299", 0, 0.5, 2),
            (12934, "Zulrah's scales", "500", 0, 0.1, 2),       # a different line
            (995, None, "1,000", 1, 0, None),                   # unparseable rate
        ]
        items = npcs.drop_table_items(rows)
        assert [i["item_id"] for i in items] == [12922, 12934, 12934, 995]
        assert items[-1] == {"item_id": 995, "item_name": None, "quantity": "1,000",
                             "noted": True, "rarity": None, "rolls": 1}


class TestFamilyCandidates:
    def test_a_base_raid_with_no_table_looks_at_its_modes(self):
        tiers = npcs.family_candidates(13699, "Theatre of Blood", _ROWS)
        assert tiers == [[13958, 13961]]

    def test_the_npc_itself_is_never_its_own_donor(self):
        for tier in npcs.family_candidates(2042, "Zulrah", _ROWS):
            assert 2042 not in tier


class TestDropTable:
    @pytest.fixture(autouse=True)
    def _clear(self):
        npcs._drop_table_cache.clear()

    def test_own_rows_win(self, monkeypatch):
        monkeypatch.setattr(npcs, "_wiki_rows", lambda s, ids: {
            2042: [(12922, "Tanzanite fang", "1", 0, 0.002, 2)]} if 2042 in ids else {})
        table = npcs._drop_table(None, 2042, "Zulrah", _ROWS)
        assert table["source"] == "wiki" and table["source_npc_id"] == 2042
        assert table["items"][0]["item_id"] == 12922

    def test_a_variant_borrows_its_family_table_and_says_from_where(self, monkeypatch):
        monkeypatch.setattr(npcs, "_wiki_rows", lambda s, ids: {
            13961: [(22486, "Scythe of vitur (uncharged)", "1", 0, 0.0526, 1)]}
            if 13961 in ids else {})
        table = npcs._drop_table(None, 13699, "Theatre of Blood", _ROWS)
        assert table["source"] == "wiki_family" and table["source_npc_id"] == 13961
        assert table["items"][0]["item_name"] == "Scythe of vitur (uncharged)"

    def test_nothing_anywhere_is_an_empty_table(self, monkeypatch):
        monkeypatch.setattr(npcs, "_wiki_rows", lambda s, ids: {})
        table = npcs._drop_table(None, 900006, "The Fractured Archive", _ROWS)
        assert table == {"source": None, "source_npc_id": None, "items": []}

    def test_a_missing_grant_is_a_null_table_not_a_500(self, monkeypatch):
        class Denied(Exception):
            pass

        def deny(_s, _ids):
            raise Denied(1142, "SELECT command denied")

        session = types.SimpleNamespace(rollback=lambda: None)
        monkeypatch.setattr(npcs, "_wiki_rows", deny)
        assert npcs._drop_table(session, 2042, "Zulrah", _ROWS) is None
        # ...and it is not cached, so applying the grant fixes it without a restart.
        assert 2042 not in npcs._drop_table_cache

    def test_any_other_error_still_raises(self, monkeypatch):
        def boom(_s, _ids):
            raise RuntimeError("timeout")

        monkeypatch.setattr(npcs, "_wiki_rows", boom)
        with pytest.raises(RuntimeError):
            npcs._drop_table(None, 2042, "Zulrah", _ROWS)


class _Decision:
    allowed = True
    reason = ""
    retry_after = None
    headers = {"X-RateLimit-Cost": "5"}


class _Slot:
    acquired = True

    def __init__(self, *_args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def client(monkeypatch):
    import data_api
    import data_api.auth as auth
    import data_api.serving as serving
    import data_api.usage as usage
    from quart import g

    async def _authenticate():
        g.api_key = {
            "key_id": 1, "label": "test", "tier": "standard",
            "scope": "group", "owner_type": "group",
            "owner_user_id": None, "group_id": 299,
            "limits": {"requests_per_min": 100, "cost_units_per_min": 10 ** 9,
                       "requests_per_day": 10 ** 6, "max_concurrency": 4},
        }
        return None

    class _Session:
        def execute(self, *_a, **_k):
            return types.SimpleNamespace(first=lambda: None)

        def close(self):
            pass

    charged = []

    def _charge(_key_id, _limits, cost):
        charged.append(cost)
        return _Decision()

    monkeypatch.setattr(auth, "authenticate_request", _authenticate)
    monkeypatch.setattr(serving, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(serving, "check_and_charge", _charge)
    monkeypatch.setattr(serving, "Concurrency", _Slot)
    monkeypatch.setattr(usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(usage, "touch_last_used", lambda *a, **k: False)
    monkeypatch.setattr(sect, "_npc_rows", lambda _s: list(_ROWS))
    monkeypatch.setattr(sect, "_boss_metric_of",
                        lambda name: "zulrah" if name == "Zulrah" else None)
    monkeypatch.setattr(sect, "resolve_npc_ids",
                        lambda _s, raw: [2042] if raw.lower() == "zulrah" else [])
    monkeypatch.setattr(npcs, "_wiki_rows", lambda s, ids: {})
    npcs._wom_metric_cache.clear()
    npcs._drop_table_cache.clear()

    app = data_api.create_app().test_client()
    app.charged = charged
    return app


async def _get(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer dtk_1_" + "a" * 32})
    return response.status_code, await response.get_json()


class TestRoutes:
    @pytest.mark.asyncio
    async def test_the_full_catalogue_sorted_by_id(self, client):
        status, body = await _get(client, "/v2/npcs")
        assert status == 200
        assert body["count"] == len(_ROWS)
        assert [n["npc_id"] for n in body["npcs"]] == sorted(r[0] for r in _ROWS)
        assert body["npcs"][0] == {"npc_id": 2042, "name": "Zulrah",
                                   "synthetic": False, "wom_metric": "zulrah"}
        assert client.charged == [npcs.NPC_LIST_COST]

    @pytest.mark.asyncio
    async def test_a_search_is_cheaper(self, client):
        status, body = await _get(client, "/v2/npcs?name=Zulrah")
        assert status == 200 and [n["npc_id"] for n in body["npcs"]] == [2042]
        status, body = await _get(client, "/v2/npcs?q=archive")
        assert [n["npc_id"] for n in body["npcs"]] == [900006]
        assert body["npcs"][0]["synthetic"] is True
        assert client.charged == [npcs.NPC_SEARCH_COST] * 2

    @pytest.mark.asyncio
    async def test_an_empty_filter_is_a_400(self, client):
        status, body = await _get(client, "/v2/npcs?q=%20")
        assert status == 400 and body["error"] == "malformed_parameter"
        assert client.charged == []

    @pytest.mark.asyncio
    async def test_one_npc(self, client):
        status, body = await _get(client, "/v2/npcs/13699")
        assert status == 200
        assert body["name"] == "Theatre of Blood"
        assert body["wiki_url"].endswith("/Theatre_of_Blood")
        assert body["icon_url"].endswith("/npcdb/13699.png")
        assert body["drop_table"] == {"source": None, "source_npc_id": None, "items": []}

    @pytest.mark.asyncio
    async def test_an_unknown_id_is_a_404(self, client):
        status, body = await _get(client, "/v2/npcs/1")
        assert status == 404 and "1" in body["detail"]


class TestPersonalBestsCarryTheName:
    def test_names_ride_along_and_a_missing_npc_row_is_null(self):
        class _Session:
            def execute(self, *_a, **_k):
                return iter([
                    (7, 2042, "Solo", 61000, "Zulrah"),
                    (7, 99999, "Solo", 50000, None),
                ])

        out = sect._load_personal_bests(_Session(), [7], {})
        assert out[7]["bests"] == [
            {"npc_id": 2042, "npc_name": "Zulrah", "team_size": "Solo", "best_ms": 61000},
            {"npc_id": 99999, "npc_name": None, "team_size": "Solo", "best_ms": 50000},
        ]
