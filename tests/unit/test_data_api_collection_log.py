"""``GET /v2/collection-log`` — the slot catalogue as reference data.

The bot integrations that award points from collection logs each shipped a
hand-built ``clog_items.json`` that went stale on every game update. This
endpoint is the replacement: the same manifest row the plugin renders from,
served to any valid key, changing only when the weekly cache refresh finds a
real change.
"""
from __future__ import annotations

import json
import types
from datetime import datetime

import pytest

from data_api.routes.reference import collection_log_payload

_TABS = [
    {"name": "Bosses", "pages": [
        {"name": "Abyssal Sire", "items": [13262, 25624, 7979],
         "names": ["Abyssal orphan", "Unsired", "Abyssal head"]},
        {"name": "Alchemical Hydra", "items": [22746, 22988],
         "names": ["Hydra's claw", "Hydra leather"]},
    ]},
    {"name": "Other", "pages": [
        # A slot that also appears on another page keeps its first name.
        {"name": "Slayer", "items": [7979, 4151], "names": ["Abyssal head (dup)", "Abyssal whip"]},
        {"name": "Empty page", "items": [], "names": []},
    ]},
]


class TestPayloadShape:
    def test_flattens_to_an_id_name_map_and_keeps_the_tabs(self):
        body = collection_log_payload(json.dumps(_TABS), datetime(2026, 8, 27, 10, 10, 4))
        assert body["items"] == {
            "13262": "Abyssal orphan", "25624": "Unsired", "7979": "Abyssal head",
            "22746": "Hydra's claw", "22988": "Hydra leather", "4151": "Abyssal whip",
        }
        assert body["slot_count"] == 7 and body["distinct_items"] == 6
        assert body["tabs"] == _TABS
        assert body["updated_at"] == "2026-08-27T10:10:04"
        assert body["updated_at_unix"] == 1787825404

    def test_a_slot_the_cache_could_not_name_is_null_not_empty_string(self):
        tabs = [{"name": "T", "pages": [{"name": "P", "items": [1, 2], "names": ["One"]}]}]
        body = collection_log_payload(json.dumps(tabs), None)
        assert body["items"] == {"1": "One", "2": None}
        assert body["updated_at"] is None and body["updated_at_unix"] is None

    def test_garbage_in_the_row_is_not_found_rather_than_a_500(self):
        assert collection_log_payload("not json", None) is None
        assert collection_log_payload(json.dumps({"tabs": []}), None) is None


class _Decision:
    allowed = True
    reason = ""
    retry_after = None
    headers = {"X-RateLimit-Cost": "20"}


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
        row = (json.dumps(_TABS), datetime(2026, 8, 27, 10, 10, 4))

        def execute(self, *_a, **_k):
            row = _Session.row

            class _Result:
                @staticmethod
                def first():
                    return row

            return _Result()

        def close(self):
            pass

    monkeypatch.setattr(auth, "authenticate_request", _authenticate)
    monkeypatch.setattr(serving, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(serving, "check_and_charge", lambda *a, **k: _Decision())
    monkeypatch.setattr(serving, "Concurrency", _Slot)
    monkeypatch.setattr(usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(usage, "touch_last_used", lambda *a, **k: False)
    monkeypatch.setattr(serving, "text", lambda q: types.SimpleNamespace(
        bindparams=lambda **kw: q), raising=False)

    app = data_api.create_app().test_client()
    app._session = _Session
    return app


async def _get(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer dtk_1_" + "a" * 32})
    return response.status_code, await response.get_json(), response.headers


class TestRoute:
    @pytest.mark.asyncio
    async def test_any_valid_key_gets_the_catalogue(self, client):
        # A group-scoped key: this is game data, not player data, so there is
        # nothing to be out of scope for.
        status, body, headers = await _get(client, "/v2/collection-log")
        assert status == 200
        assert body["items"]["4151"] == "Abyssal whip"
        assert body["slot_count"] == 7
        assert headers["Cache-Control"].startswith("private, max-age=")

    @pytest.mark.asyncio
    async def test_nothing_published_is_a_404_that_says_so(self, client):
        client._session.row = None
        status, body, _headers = await _get(client, "/v2/collection-log")
        assert status == 404
        assert "collection log" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_it_still_needs_a_key(self, client, monkeypatch):
        import data_api.auth as auth
        from quart import jsonify

        async def _reject():
            return jsonify({"error": "unauthorized"}), 401

        monkeypatch.setattr(auth, "authenticate_request", _reject)
        response = await client.get("/v2/collection-log")
        assert response.status_code == 401
