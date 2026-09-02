"""The drop feed's request parameters, end to end through the real routes.

``since``/``until`` are unix seconds; the window defaults to the last 24 hours
and is capped at 7 days by clamping (the published ceiling), while a window
that runs backwards is refused. None of it is parsed unless ``drops`` was
asked for, so a caller of the profile sections is never refused over a
parameter they did not use.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta

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
def harness(monkeypatch):
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

    seen = {}

    def _load_sections(_s, sections, ids, ctx):
        seen["ctx"] = ctx
        return {pid: {"player_id": pid} for pid in ids}

    charged = {}

    def _charge(_key, _limits, cost):
        charged["cost"] = cost
        return _Decision()

    monkeypatch.setattr(auth, "authenticate_request", _authenticate)
    monkeypatch.setattr(serving, "SessionLocal", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(serving, "check_and_charge", _charge)
    monkeypatch.setattr(serving, "Concurrency", _Slot)
    monkeypatch.setattr(usage, "record", lambda *a, **k: None)
    monkeypatch.setattr(usage, "touch_last_used", lambda *a, **k: False)
    monkeypatch.setattr(scope, "group_exists", lambda _s, gid: gid == 7)
    monkeypatch.setattr(scope, "group_roster_page", lambda _s, gid, _c, _l: [11, 12] if gid == 7 else [])
    monkeypatch.setattr(sect, "load_sections", _load_sections)
    monkeypatch.setattr(sect, "resolve_npc_ids",
                        lambda _s, raw: [415] if raw == "abyssal_sire" else [])

    return data_api.create_app().test_client(), seen, charged


async def _get(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer dtk_1_" + "a" * 32})
    return response.status_code, await response.get_json()


def _hours(ctx):
    since, until = ctx["drops_window"]
    return (until - since).total_seconds() / 3600


class TestWindow:
    @pytest.mark.asyncio
    async def test_defaults_to_the_last_24_hours_ending_now(self, harness):
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/groups/7/players?include=drops")
        assert status == 200
        assert _hours(seen["ctx"]) == 24
        assert datetime.utcnow() - seen["ctx"]["drops_window"][1] < timedelta(seconds=5)
        assert seen["ctx"]["drops_per_player"] == 50

    @pytest.mark.asyncio
    async def test_explicit_bounds_are_honoured(self, harness):
        client, seen, _ = harness
        status, _ = await _get(client,
                               "/v2/groups/7/players?include=drops&since=1788177600&until=1788264000")
        assert status == 200
        assert seen["ctx"]["drops_window"] == (datetime(2026, 8, 31, 12), datetime(2026, 9, 1, 12))

    @pytest.mark.asyncio
    async def test_a_window_past_seven_days_is_clamped_not_refused(self, harness):
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/groups/7/players?include=drops&since=0")
        assert status == 200
        assert _hours(seen["ctx"]) == 24 * 7

    @pytest.mark.asyncio
    async def test_until_in_the_future_is_clamped_to_now(self, harness):
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/groups/7/players?include=drops&until=4102444800")
        assert status == 200
        assert seen["ctx"]["drops_window"][1] <= datetime.utcnow()

    @pytest.mark.asyncio
    async def test_a_backwards_window_is_a_400(self, harness):
        client, _, _ = harness
        status, body = await _get(client,
                                  "/v2/groups/7/players?include=drops&since=1788264000&until=1788177600")
        assert status == 400
        assert body["error"] == "malformed_parameter"
        assert "since" in body["detail"]

    @pytest.mark.parametrize("query", ["since=abc", "until=abc", "max_drops=abc"])
    @pytest.mark.asyncio
    async def test_non_integer_parameters_are_named(self, harness, query):
        client, _, _ = harness
        status, body = await _get(client, f"/v2/groups/7/players?include=drops&{query}")
        assert status == 400
        assert query.split("=")[0] in body["detail"]

    @pytest.mark.asyncio
    async def test_max_drops_is_clamped_to_the_ceiling(self, harness):
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/groups/7/players?include=drops&max_drops=99999")
        assert status == 200
        assert seen["ctx"]["drops_per_player"] == 200

    @pytest.mark.asyncio
    async def test_parameters_are_ignored_without_the_feed(self, harness):
        # A profile request carrying a broken `since` is still served: the
        # parameter belongs to a section that was not requested.
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/groups/7/players?include=loot&since=abc")
        assert status == 200
        assert "drops_window" not in seen["ctx"]


class TestPricing:
    @pytest.mark.asyncio
    async def test_a_week_costs_seven_days(self, harness):
        import data_api.sections as sect

        client, _, charged = harness
        await _get(client, "/v2/groups/7/players?include=drops")
        day = charged["cost"]
        await _get(client, "/v2/groups/7/players?include=drops&since=0")
        assert charged["cost"] == day * 7
        assert day == sect.REGISTRY["drops"].cost * 2  # two players on the page


class TestNpcFilter:
    @pytest.mark.asyncio
    async def test_a_known_boss_reaches_the_loader_as_ids(self, harness):
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/groups/7/players?include=drops&npc=abyssal_sire")
        assert status == 200
        assert seen["ctx"]["drops_npc_ids"] == [415]

    @pytest.mark.asyncio
    async def test_an_unknown_boss_is_a_400_that_names_it(self, harness):
        client, _, charged = harness
        status, body = await _get(client, "/v2/groups/7/players?include=drops&npc=nope")
        assert status == 400
        assert body["error"] == "malformed_parameter"
        assert "nope" in body["detail"] and "barrows_chests" in body["detail"]
        assert "cost" not in charged, "a caller's typo must not be billed"

    @pytest.mark.asyncio
    async def test_a_blank_npc_is_a_400(self, harness):
        client, _, _ = harness
        status, body = await _get(client, "/v2/groups/7/players?include=drops&npc=")
        assert status == 400
        assert "npc" in body["detail"]

    @pytest.mark.asyncio
    async def test_the_filter_is_ignored_without_the_feed(self, harness):
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/groups/7/players?include=loot&npc=nope")
        assert status == 200
        assert "drops_npc" not in seen["ctx"]

    @pytest.mark.asyncio
    async def test_the_filter_does_not_change_the_charge(self, harness):
        client, _, charged = harness
        await _get(client, "/v2/groups/7/players?include=drops")
        unfiltered = charged["cost"]
        await _get(client, "/v2/groups/7/players?include=drops&npc=abyssal_sire")
        assert charged["cost"] == unfiltered

    @pytest.mark.asyncio
    async def test_the_single_player_route_takes_the_filter_too(self, harness, monkeypatch):
        # The bot walks a truncated player back through /v2/players/<id>.
        import data_api.scope as scope

        monkeypatch.setattr(scope, "resolve_player_ref", lambda _s, ref: 11)
        monkeypatch.setattr(scope, "visible_player_ids", lambda _s, ids: ids)
        monkeypatch.setattr(scope, "key_may_read", lambda *_a: True)
        client, seen, _ = harness
        status, _ = await _get(client, "/v2/players/11?include=drops&npc=abyssal_sire")
        assert status == 200
        assert seen["ctx"]["drops_npc_ids"] == [415]
