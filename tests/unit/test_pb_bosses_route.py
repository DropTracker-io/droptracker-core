"""Unit tests for api/routes/personal_bests.py (the public /pb_bosses catalogue).

The catalogue builder is exercised against a stubbed DB fetch so the
grouping / normalization / blocklist / sort rules are covered without a live
MySQL connection.
"""

import pytest

from api.routes import personal_bests as pb


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
class _FakeSession:
    """Minimal stand-in for the SQLAlchemy session get_db_session() returns."""

    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def execute(self, _sql):
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


@pytest.fixture
def build(monkeypatch):
    """Return a callable that builds a catalogue from (npc_id, name, team_size)."""

    def _build(rows, blocked=()):
        sessions = []

        def _fake_get_db_session(*_a, **_kw):
            s = _FakeSession(rows)
            sessions.append(s)
            return s

        monkeypatch.setattr(pb, "get_db_session", _fake_get_db_session)
        monkeypatch.setattr(pb, "get_blocked_ids", lambda: set(blocked))
        result = pb._build_catalogue()
        assert sessions and all(s.closed for s in sessions), "session not closed"
        return result

    return _build


# --------------------------------------------------------------------------- #
# Team-size ordering
# --------------------------------------------------------------------------- #
class TestTeamSizeSortKey:
    def test_solo_sorts_before_every_numeric_size(self):
        assert sorted(["4", "2", "Solo", "10"], key=pb._team_size_sort_key) == [
            "Solo", "2", "4", "10",
        ]

    def test_exact_sizes_sort_numerically_not_lexically(self):
        assert sorted(["10", "9", "2"], key=pb._team_size_sort_key) == ["2", "9", "10"]

    def test_ranges_and_open_brackets_sort_after_exact_sizes(self):
        got = sorted(["11-15", "6+", "24", "Solo", "3"], key=pb._team_size_sort_key)
        assert got == ["Solo", "3", "24", "6+", "11-15"]

    def test_unrecognised_tokens_sort_last(self):
        got = sorted(["Trio", "Solo", "5"], key=pb._team_size_sort_key)
        assert got == ["Solo", "5", "Trio"]


# --------------------------------------------------------------------------- #
# Query-flag parsing
# --------------------------------------------------------------------------- #
class TestTruthy:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "y", "on", " On "])
    def test_accepted_truthy_spellings(self, raw):
        assert pb._truthy(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  ", None, "2"])
    def test_everything_else_is_false(self, raw):
        assert pb._truthy(raw) is False


# --------------------------------------------------------------------------- #
# Catalogue assembly
# --------------------------------------------------------------------------- #
class TestBuildCatalogue:
    def test_groups_team_sizes_under_one_boss(self, build):
        got = build([
            (13699, "Theatre of Blood", "Solo"),
            (13699, "Theatre of Blood", "4"),
            (13699, "Theatre of Blood", "5"),
        ])
        assert len(got) == 1
        assert got[0]["npc_id"] == 13699
        assert got[0]["team_sizes"] == ["Solo", "4", "5"]

    def test_legacy_team_size_spellings_collapse_to_one_board(self, build):
        # "(4 scale)" truncations and a bare "4" are the same board.
        got = build([
            (13696, "Chambers of Xeric", "4"),
            (13696, "Chambers of Xeric", "(4"),
            (13696, "Chambers of Xeric", "4 s"),
            (13696, "Chambers of Xeric", "1"),
        ])
        assert got[0]["team_sizes"] == ["Solo", "4"]

    def test_blocklisted_npcs_are_dropped(self, build):
        rows = [
            (13699, "Theatre of Blood", "Solo"),
            (2215, "Bugged NPC", "Solo"),
        ]
        got = build(rows, blocked={2215})
        assert [b["npc_id"] for b in got] == [13699]

    def test_unusable_team_size_tokens_are_skipped(self, build):
        got = build([
            (13699, "Theatre of Blood", "Solo"),
            (13699, "Theatre of Blood", "0"),
        ])
        assert got[0]["team_sizes"] == ["Solo"]

    def test_boss_with_only_unusable_tokens_is_dropped(self, build):
        # "0" is the only spelling on record, so the boss has no ranked board.
        # web_api's _build_dataset drops it from the site leaderboards for the
        # same reason; the catalogue must not advertise a boss with no boards.
        assert build([(1234, "Odd NPC", "0")]) == []

    def test_listed_bosses_always_have_at_least_one_team_size(self, build):
        got = build([
            (13699, "Theatre of Blood", "0"),
            (13699, "Theatre of Blood", "5"),
            (1234, "Odd NPC", "0"),
        ])
        assert got and all(b["team_sizes"] for b in got)

    def test_bosses_sorted_by_name(self, build):
        got = build([
            (13699, "Theatre of Blood", "Solo"),
            (8615, "Alchemical Hydra", "Solo"),
            (13695, "Tombs of Amascut", "Solo"),
        ])
        assert [b["npc_name"] for b in got] == [
            "Alchemical Hydra", "Theatre of Blood", "Tombs of Amascut",
        ]

    def test_icon_url_points_at_the_npc_image_path(self, build):
        got = build([(13696, "Chambers of Xeric", "Solo")])
        assert got[0]["icon_url"] == "https://www.droptracker.io/img/npcdb/13696.png"

    def test_empty_table_yields_empty_catalogue(self, build):
        assert build([]) == []


# --------------------------------------------------------------------------- #
# Route behaviour
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    from quart import Quart
    from quart_rate_limiter import RateLimiter

    monkeypatch.setattr(pb, "_catalogue", lambda: [
        {
            "npc_id": 13696,
            "npc_name": "Chambers of Xeric",
            "icon_url": "https://www.droptracker.io/img/npcdb/13696.png",
            "team_sizes": ["Solo", "4"],
        },
    ])

    app = Quart(__name__)
    RateLimiter(app)
    app.register_blueprint(pb.personal_bests_bp, url_prefix="/")
    return app.test_client()


class TestRoute:
    @pytest.mark.asyncio
    async def test_sizes_omitted_by_default(self, client):
        resp = await client.get("/pb_bosses")
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body["count"] == 1
        assert "team_sizes" not in body["bosses"][0]
        assert body["bosses"][0]["npc_id"] == 13696

    @pytest.mark.asyncio
    @pytest.mark.parametrize("qs", ["withSizes=1", "with_sizes=1", "withSizes=true"])
    async def test_sizes_included_when_requested(self, client, qs):
        resp = await client.get(f"/pb_bosses?{qs}")
        body = await resp.get_json()
        assert body["bosses"][0]["team_sizes"] == ["Solo", "4"]

    @pytest.mark.asyncio
    async def test_falsey_flag_omits_sizes(self, client):
        resp = await client.get("/pb_bosses?withSizes=0")
        body = await resp.get_json()
        assert "team_sizes" not in body["bosses"][0]

    @pytest.mark.asyncio
    async def test_response_is_publicly_cacheable(self, client):
        resp = await client.get("/pb_bosses")
        assert resp.headers["Cache-Control"] == "public, max-age=300"

    @pytest.mark.asyncio
    async def test_stripping_sizes_does_not_mutate_the_cached_catalogue(self, client):
        # The no-flag path filters keys; if it popped from the cached dicts the
        # next withSizes=1 request would come back empty.
        await client.get("/pb_bosses")
        resp = await client.get("/pb_bosses?withSizes=1")
        body = await resp.get_json()
        assert body["bosses"][0]["team_sizes"] == ["Solo", "4"]
