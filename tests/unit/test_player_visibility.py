"""`GET /players/{id}/visibility` — the always-200 privacy check (ticket #515).

The profile 404s a hidden account, and Next's fetch cache only stores 200s, so
the web kept serving the pre-hide profile by id. This route answers 200 either
way, and missing and hidden must read the same.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from web_api.routes import profiles


class _SessionCM:
    def __init__(self, row):
        self.s = MagicMock()
        self.s.execute.return_value = MagicMock(fetchone=lambda: row)

    def __enter__(self):
        return self.s

    def __exit__(self, *a):
        return False


@pytest.fixture()
def client():
    import web_api

    return web_api.create_app().test_client()


@pytest.mark.parametrize(
    "row, visible",
    [
        ((0, 0), True),
        ((None, None), True),  # players.hidden is NULL on older rows, user unlinked
        ((1, 0), False),       # account hidden
        ((0, 1), False),       # "Hide me everywhere"
        ((None, 1), False),
        (None, False),         # no such player: indistinguishable from hidden
    ],
)
async def test_visibility(client, monkeypatch, row, visible):
    monkeypatch.setattr(profiles, "db_session", lambda: _SessionCM(row))
    r = await client.get("/api/v1/players/2266/visibility")
    assert r.status_code == 200
    assert await r.get_json() == {"id": 2266, "visible": visible}
    assert "max-age" in r.headers.get("cache-control", "")
