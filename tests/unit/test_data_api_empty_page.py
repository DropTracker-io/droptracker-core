"""A page with nobody on it must not send the loaders to the database.

``/v2/groups/{id}/players`` and ``/v2/players`` resolve to an empty id list
whenever there is no one to show: a group whose members are all hidden, or a
cursor past the last player. The second is routine. ``next_cursor`` is set
whenever a page comes back full, so a walk over a roster that is an exact
multiple of ``limit`` always ends on an empty page.

Every loader filters on ``player_id IN :ids``, and an empty tuple renders as
``IN ()``. MariaDB rejects that as a syntax error (1064) instead of returning
no rows. The response still came out right, because the per-player retry
looped over no one, but each empty page cost one failed query and one logged
traceback per requested section. An empty page now returns before any loader
runs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import data_api.sections as sect


class _SyntaxError(Exception):
    """What PyMySQL raises for ``IN ()``: a 1064, which is not a statement timeout."""

    def __init__(self):
        super().__init__(1064, "You have an error in your SQL syntax; check the "
                               "manual ... near ')' at line 6")


def _failing_session():
    """A session that rejects every statement the way MariaDB rejects ``IN ()``.

    A regression then plays out as it did in production (the page query fails,
    is logged, rolled back and retried per player) instead of passing quietly
    on a mock that returns no rows.
    """
    session = MagicMock()
    session.execute.side_effect = _SyntaxError()
    return session


def _ctx(sections):
    """The context ``serving.serve`` builds, with every key a loader indexes."""
    until = datetime.utcnow().replace(microsecond=0)
    return {
        "date_hour_range": ((until - timedelta(days=30)).strftime("%Y-%m-%d-%H"),
                            until.strftime("%Y-%m-%d-%H")),
        "days": 30,
        "sections": sections,
        "per_player_limit": 10,
        "drops_window": (until - timedelta(days=7), until),
        "drops_per_player": 50,
        "partition": 202609,
    }


class TestAnEmptyPageLoadsNothing:
    def test_no_section_queries_rolls_back_or_logs(self, caplog):
        # Every registered section, including the ones `all` leaves out.
        sections = list(sect.ALL_SECTION_KEYS)
        session = _failing_session()

        with caplog.at_level(logging.DEBUG, logger=sect.logger.name):
            merged = sect.load_sections(session, sections, [], _ctx(sections))

        assert merged == {}
        assert [r.getMessage() for r in caplog.records if r.name == sect.logger.name] == []
        session.execute.assert_not_called()
        session.rollback.assert_not_called()

    def test_a_page_with_players_still_queries(self):
        # The guard is about emptiness: one player on the page reaches the loader.
        session = MagicMock()
        merged = sect.load_sections(session, ["identity"], [11], _ctx(["identity"]))

        session.execute.assert_called_once()
        assert merged == {11: {}}


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
    """The real app, routes, ``serve`` and loader path. Only the edges are stubbed.

    ``load_sections`` is left real and the session is the failing one above, so
    a loader that reaches the database fails the test.
    """
    import data_api
    import data_api.auth as auth
    import data_api.routes.groups as groups_routes
    import data_api.scope as scope
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

    session = _failing_session()
    charged, recorded = [], []

    def _charge(_key_id, _limits, cost):
        charged.append(cost)
        return _Decision()

    def _record(_key_id, endpoint, status, _duration_ms, _cost, players=1, limited=False):
        recorded.append((endpoint, status, players))

    monkeypatch.setattr(auth, "authenticate_request", _authenticate)
    monkeypatch.setattr(serving, "SessionLocal", lambda: session)
    monkeypatch.setattr(serving, "check_and_charge", _charge)
    monkeypatch.setattr(serving, "Concurrency", _Slot)
    monkeypatch.setattr(usage, "record", _record)
    monkeypatch.setattr(usage, "touch_last_used", lambda *a, **k: False)
    # Group 7 exists, and both listings are past their last player.
    monkeypatch.setattr(scope, "group_exists", lambda _s, gid: gid == 7)
    monkeypatch.setattr(scope, "group_roster_page", lambda *_a: [])
    monkeypatch.setattr(scope, "all_players_page", lambda *_a: [])
    # The group's own block (with meta) is about the group, not the page, and
    # queries a real group row, so it is stubbed rather than failed.
    monkeypatch.setattr(groups_routes, "_group_block",
                        lambda _s, gid, _ctx, with_all_time: {"group_id": gid, "name": "Seven"})

    return data_api.create_app().test_client(), session, charged, recorded


async def _get(client, path):
    response = await client.get(path, headers={"Authorization": "Bearer dtk_1_" + "a" * 32})
    return response.status_code, await response.get_json()


#: The include= shapes production logged ending on an empty page.
_SHAPES = ("meta", "loot_items", "personal_bests", "drops")


class TestAnEmptyPageStillAnswers:
    @pytest.mark.parametrize("include", _SHAPES)
    @pytest.mark.asyncio
    async def test_a_group_roster_past_its_last_member(self, harness, include):
        client, session, charged, recorded = harness
        status, body = await _get(client, f"/v2/groups/7/players?include={include}&cursor=1500")

        sections = ["identity", include]
        expected = {"group_id": 7, "count": 0, "sections": sections,
                    "next_cursor": None, "players": []}
        if include == "meta":
            expected["group"] = {"group_id": 7, "name": "Seven"}
        assert status == 200
        assert body == expected
        # Still priced and charged once, before the work, for a zero-player page.
        assert charged == [sect.cost_of(sections, 0)]
        assert recorded == [("groups.players", 200, 0)]
        session.execute.assert_not_called()
        session.close.assert_called_once()

    @pytest.mark.parametrize("include", _SHAPES)
    @pytest.mark.asyncio
    async def test_the_player_listing_past_its_last_player(self, harness, include):
        client, session, charged, recorded = harness
        status, body = await _get(client, f"/v2/players?include={include}&cursor=99999999")

        sections = ["identity", include]
        assert status == 200
        assert body == {"count": 0, "sections": sections, "next_cursor": None, "players": []}
        assert charged == [sect.cost_of(sections, 0)]
        assert recorded == [("players.list", 200, 0)]
        session.execute.assert_not_called()
        session.close.assert_called_once()
