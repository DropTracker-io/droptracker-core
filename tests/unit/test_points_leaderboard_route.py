"""GET /groups/{id}/points/leaderboard -- the public points board.

The handler is driven end to end with a real SQLite session behind
``db_session``, so the rows it serves are whatever ``db/point_standings.py``'s
real SQL produced: leavers gone, a Discord user's RSNs summed when the group
asks for it, a search that narrows without re-ranking. Only the ORM-backed
helpers around it (group lookup, behavior read, seasons) are replaced -- they
are MagicMocks under the stubbed ``db`` package and are not what is under test.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import web_api.routes.points as pts
from db.point_standings import COMBINE_CONFIG_KEY

from tests.unit.test_point_standings import (  # noqa: F401  (fixtures)
    GROUP,
    _award,
    _join,
    _leave,
    _player,
    _user,
    clan,
    session,
)


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


def _wire(monkeypatch, sess, *, behavior=None, viewer_id=None, role=None):
    @contextmanager
    def _cm():
        yield sess

    merged = dict(pts.BEHAVIOR_DEFAULTS)
    merged.update(behavior or {})
    monkeypatch.setattr(pts, "db_session", _cm)
    monkeypatch.setattr(pts, "optional_user_id", lambda: viewer_id)
    monkeypatch.setattr(
        pts, "_assert_group_exists",
        lambda s, gid: SimpleNamespace(group_id=gid, group_name="Test Clan"),
    )
    monkeypatch.setattr(pts, "_read_behavior", lambda s, gid: merged)
    monkeypatch.setattr(pts, "_seasons_payload", lambda s, gid: [])
    monkeypatch.setattr(pts, "manageable_guild_ids", lambda uid: [])
    monkeypatch.setattr(pts, "resolve_group_role", lambda s, uid, gid, guilds: role)


async def _get(client, query=""):
    resp = await client.get(f"/api/v1/groups/{GROUP}/points/leaderboard?period=all{query}")
    return resp.status_code, await resp.get_json()


def test_the_behavior_key_is_the_one_the_standings_module_reads():
    # The route writes the toggle, the shared module reads it for the bot and
    # the award engine; each keeps its own copy of the name.
    assert COMBINE_CONFIG_KEY in pts.BEHAVIOR_BOOL_KEYS
    assert pts.BEHAVIOR_DEFAULTS[COMBINE_CONFIG_KEY] is False


async def test_per_rsn_board(client, monkeypatch, clan):
    _wire(monkeypatch, clan)
    status, body = await _get(client)
    assert status == 200
    assert body["combined"] is False
    assert [(e["rank"], e["name"], e["points"]) for e in body["entries"]] == [
        (1, "Solo", 100), (2, "Alt", 80), (3, "Main", 50),
    ]
    assert body["entries"][0]["accounts"] == [{"id": 3, "name": "Solo", "points": 100}]
    assert body["meta"]["total"] == 3


async def test_combined_board_lists_the_accounts_behind_a_total(client, monkeypatch, clan):
    _wire(monkeypatch, clan, behavior={"points_combine_accounts": True})
    _status, body = await _get(client)
    assert body["combined"] is True
    top = body["entries"][0]
    assert (top["rank"], top["id"], top["name"], top["points"]) == (1, 2, "Alt", 130)
    assert top["accounts"] == [
        {"id": 2, "name": "Alt", "points": 80},
        {"id": 1, "name": "Main", "points": 50},
    ]
    assert body["meta"]["total"] == 2


async def test_a_leaver_is_not_on_the_board(client, monkeypatch, clan):
    _leave(clan, 3)
    _wire(monkeypatch, clan)
    _status, body = await _get(client)
    assert [e["name"] for e in body["entries"]] == ["Alt", "Main"]
    assert body["meta"]["total"] == 2


async def test_the_alt_that_left_stops_counting_on_a_combined_board(client, monkeypatch, clan):
    _leave(clan, 2)
    _wire(monkeypatch, clan, behavior={"points_combine_accounts": True})
    _status, body = await _get(client)
    assert [(e["name"], e["points"]) for e in body["entries"]] == [("Solo", 100), ("Main", 50)]


async def test_search_narrows_without_re_ranking(client, monkeypatch, clan):
    _wire(monkeypatch, clan)
    _status, body = await _get(client, "&q=main")
    assert [(e["rank"], e["name"]) for e in body["entries"]] == [(3, "Main")]
    assert body["meta"]["total"] == 1
    assert body["query"] == "main"


async def test_search_finds_a_combined_entry_by_its_alt(client, monkeypatch, clan):
    _wire(monkeypatch, clan, behavior={"points_combine_accounts": True})
    _status, body = await _get(client, "&q=mai")
    assert [(e["rank"], e["name"]) for e in body["entries"]] == [(1, "Alt")]


async def test_search_with_no_hits_is_an_empty_page_not_an_error(client, monkeypatch, clan):
    _wire(monkeypatch, clan)
    status, body = await _get(client, "&q=nobody")
    assert status == 200
    assert body["entries"] == [] and body["meta"]["total"] == 0


async def test_an_overlong_query_is_trimmed_not_rejected(client, monkeypatch, clan):
    _wire(monkeypatch, clan)
    status, body = await _get(client, "&q=" + "x" * 200)
    assert status == 200
    assert len(body["query"]) == pts.MAX_LEADERBOARD_QUERY


async def test_hidden_player_keeps_a_rank_gap_and_pages_stay_full(client, monkeypatch, session):
    _player(session, 1, "Ghost", hidden=1)
    _player(session, 2, "Second")
    _player(session, 3, "Third")
    for pid, amount in ((1, 90), (2, 50), (3, 10)):
        _join(session, pid)
        _award(session, pid, amount)
    _wire(monkeypatch, session)
    _status, body = await _get(client, "&limit=2")
    # Ghost holds rank 1 but is never listed; the page still carries 2 rows.
    assert [(e["rank"], e["name"]) for e in body["entries"]] == [(2, "Second"), (3, "Third")]
    assert body["meta"]["total"] == 2


async def test_hidden_name_is_not_searchable(client, monkeypatch, session):
    _player(session, 1, "Ghost", hidden=1)
    _join(session, 1)
    _award(session, 1, 90)
    _wire(monkeypatch, session)
    _status, body = await _get(client, "&q=ghost")
    assert body["entries"] == []


async def test_paging(client, monkeypatch, session):
    for pid in range(1, 8):
        _player(session, pid, f"P{pid}")
        _join(session, pid)
        _award(session, pid, 100 - pid)
    _wire(monkeypatch, session)
    _status, body = await _get(client, "&limit=3&page=3")
    assert [(e["rank"], e["name"]) for e in body["entries"]] == [(7, "P7")]
    assert body["meta"] == {"page": 3, "limit": 3, "total": 7}


async def test_private_board_refuses_anonymous_viewers(client, monkeypatch, clan):
    _wire(monkeypatch, clan, behavior={"points_leaderboard_public": False})
    status, _body = await _get(client)
    assert status == 403


async def test_private_board_refuses_non_members(client, monkeypatch, clan):
    _wire(monkeypatch, clan, behavior={"points_leaderboard_public": False}, viewer_id=5)
    status, _body = await _get(client)
    assert status == 403


async def test_private_board_admits_members(client, monkeypatch, clan):
    _wire(monkeypatch, clan, behavior={"points_leaderboard_public": False},
          viewer_id=5, role="member")
    status, body = await _get(client)
    assert status == 200 and len(body["entries"]) == 3


async def test_public_board_is_briefly_cacheable(client, monkeypatch, clan):
    _wire(monkeypatch, clan)
    resp = await client.get(f"/api/v1/groups/{GROUP}/points/leaderboard?period=all")
    assert "public" in resp.headers["Cache-Control"]


async def test_members_only_board_is_never_labelled_cacheable(client, monkeypatch, clan):
    # It was authorised for this one viewer.
    _wire(monkeypatch, clan, behavior={"points_leaderboard_public": False},
          viewer_id=5, role="member")
    resp = await client.get(f"/api/v1/groups/{GROUP}/points/leaderboard?period=all")
    assert resp.status_code == 200
    assert "no-store" in resp.headers["Cache-Control"]
    assert "public" not in resp.headers["Cache-Control"]


async def test_malformed_partition_is_a_400(client, monkeypatch, clan):
    _wire(monkeypatch, clan)
    resp = await client.get(f"/api/v1/groups/{GROUP}/points/leaderboard?period=20261399")
    assert resp.status_code == 400
