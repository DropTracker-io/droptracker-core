"""An unknown group id must not read as a group with nobody in it.

``/v2/groups/{id}/players`` resolved straight to a roster page, and a roster
page for a group that does not exist is simply empty — indistinguishable from
a real group whose members are all hidden. Both returned ``200 {"count": 0,
"players": []}``, so a caller probing a typo'd id got a successful answer to a
question about nothing. The same request pipeline also swallowed malformed
query parameters (silently substituting the default) except ``?top=``, which
raised straight through to a 500.
"""
from __future__ import annotations

import types

import pytest


class _Decision:
    allowed = True
    reason = ""
    retry_after = None
    headers = {"X-RateLimit-Cost": "0"}


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
    """The real app and real routes; only the DB/Redis edges are stubbed."""
    import data_api
    import data_api.auth as auth
    import data_api.scope as scope
    import data_api.sections as sect
    import data_api.serving as serving
    import data_api.usage as usage
    from quart import g

    async def _authenticate():
        g.api_key = {
            "key_id": 1, "label": "test", "tier": "partner",
            "scope": "global", "owner_type": "global",
            "owner_user_id": None, "group_id": None,
            "limits": {"requests_per_min": 100, "cost_units_per_min": 10 ** 9,
                       "requests_per_day": 10 ** 6, "max_concurrency": 4},
        }
        return None

    monkeypatch.setattr(auth, "authenticate_request", _authenticate)
    monkeypatch.setattr(serving, "SessionLocal", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(serving, "check_and_charge", lambda *a, **k: _Decision())
    monkeypatch.setattr(serving, "Concurrency", _Slot)
    monkeypatch.setattr(usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(usage, "touch_last_used", lambda *a, **k: False)

    #: group 7 exists with one member; nothing else does.
    monkeypatch.setattr(scope, "group_exists", lambda _s, gid: gid == 7)
    monkeypatch.setattr(scope, "group_roster_page",
                        lambda _s, gid, _c, _l: [11] if gid == 7 else [])
    monkeypatch.setattr(sect, "load_sections",
                        lambda _s, _sections, ids, _ctx: {pid: {"player_id": pid} for pid in ids})

    return data_api.create_app().test_client()


async def _get(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer dtk_1_" + "a" * 32})
    return response.status_code, await response.get_json()


class TestUnknownGroupIs404:
    @pytest.mark.asyncio
    async def test_a_group_that_does_not_exist_is_not_an_empty_roster(self, client):
        status, body = await _get(client, "/v2/groups/999999/players")
        assert status == 404
        assert body["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_the_404_says_which_thing_was_missing(self, client):
        _status, body = await _get(client, "/v2/groups/999999/players")
        assert "group" in body.get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_a_real_group_still_serves_its_roster(self, client):
        status, body = await _get(client, "/v2/groups/7/players")
        assert status == 200
        assert body["count"] == 1

    @pytest.mark.asyncio
    async def test_an_existing_group_with_no_visible_members_is_still_200(
            self, client, monkeypatch):
        # The distinction the fix exists to make: "nobody to show" is an
        # answer, "no such group" is not.
        import data_api.scope as scope

        monkeypatch.setattr(scope, "group_exists", lambda _s, _gid: True)
        monkeypatch.setattr(scope, "group_roster_page", lambda *_a: [])
        status, body = await _get(client, "/v2/groups/7/players")
        assert status == 200
        assert body["count"] == 0 and body["players"] == []

    @pytest.mark.asyncio
    async def test_the_pseudo_group_is_still_403_not_404(self, client):
        # Refusing group 2 is a policy, not a missing row — and it must be
        # refused before any existence lookup happens.
        status, body = await _get(client, "/v2/groups/2/players")
        assert status == 403
        assert body["error"] == "forbidden"


class TestMalformedParametersAre400:
    @pytest.mark.parametrize("query", ["limit=abc", "cursor=abc", "days=abc", "top=abc"])
    @pytest.mark.asyncio
    async def test_a_non_integer_parameter_is_named_in_a_400(self, client, query):
        status, body = await _get(client, f"/v2/groups/7/players?{query}")
        assert status == 400, f"?{query} should not be silently ignored"
        assert body["error"] == "malformed_parameter"
        assert query.split("=")[0] in body["detail"]

    @pytest.mark.asyncio
    async def test_an_out_of_range_value_is_clamped_not_refused(self, client):
        # The published maximum is a ceiling, not a typo — clamping keeps
        # working callers working.
        status, _body = await _get(client, "/v2/groups/7/players?limit=5000&days=99999")
        assert status == 200

    @pytest.mark.asyncio
    async def test_an_unknown_section_is_still_400(self, client):
        status, body = await _get(client, "/v2/groups/7/players?include=nope")
        assert status == 400
        assert body["error"] == "unknown_section"
