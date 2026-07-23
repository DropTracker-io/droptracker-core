"""Unit/contract tests for the Web API v1 foundation (Tasks 01/02/03).

The global stubs in tests/conftest.py replace ``db`` and ``utils.redis`` with
MagicMocks, so these tests exercise the framework wiring, response conventions,
and session tokens without a live DB/Redis.
"""

import pytest


# ── Config long_value precedence (Hall of Fame boss list) ─────────────────────

class TestEffectiveStoredValue:
    """Mirrors the HoF parser (services/hall_of_fame.py _parse_group_boss_list):
    config_value wins unless empty/<10 chars, then long_value."""

    def _row(self, key, config_value, long_value=None):
        class Row:
            pass

        r = Row()
        r.config_key = key
        r.config_value = config_value
        r.long_value = long_value
        return r

    def test_long_list_lives_in_long_value(self):
        from web_api.routes.config import _effective_stored_value

        long = ",".join(f"Boss {i}" for i in range(40))  # > 255 chars
        row = self._row("personal_best_embed_boss_list", "", long)
        assert _effective_stored_value(row) == long

    def test_short_config_value_falls_back_to_long_value(self):
        from web_api.routes.config import _effective_stored_value

        row = self._row("personal_best_embed_boss_list", "Zulrah", "Zulrah, Vorkath")
        assert _effective_stored_value(row) == "Zulrah, Vorkath"

    def test_fitting_config_value_wins(self):
        from web_api.routes.config import _effective_stored_value

        row = self._row("personal_best_embed_boss_list", "Zulrah, Vorkath", "stale old value")
        assert _effective_stored_value(row) == "Zulrah, Vorkath"

    def test_other_keys_never_touch_long_value(self):
        from web_api.routes.config import _effective_stored_value

        row = self._row("notify_pbs", "1", "junk")
        assert _effective_stored_value(row) == "1"


# ── Response conventions (Task 01) ────────────────────────────────────────────

class TestConventions:
    def test_money_envelope(self):
        from web_api.common import money

        assert money(2_000_000_000) == {"value": 2_000_000_000, "value_formatted": "2.00B"}
        assert money(0) == {"value": 0, "value_formatted": "0"}
        assert money(None) == {"value": 0, "value_formatted": "0"}

    def test_format_number(self):
        from web_api.common import format_number

        assert format_number(1500) == "1.50K"
        assert format_number(2_500_000) == "2.50M"
        assert format_number(999) == "999"

    def test_period_to_partition_monthly(self):
        from web_api.common import period_to_partition, get_current_partition

        assert period_to_partition("202606") == 202606
        # Unsupported forms fall back to current month (documented until Task 07).
        assert period_to_partition("all") == get_current_partition()
        assert period_to_partition("20260617") == get_current_partition()

    def test_leaderboard_key_scheme(self):
        from web_api.common import leaderboard_key

        assert leaderboard_key(202606) == "leaderboard:202606"
        assert leaderboard_key(202606, group_id=42) == "leaderboard:202606:group:42"
        assert leaderboard_key(202606, npc_id=7) == "leaderboard:202606:npc:7"

    def test_npc_key_matches_populated_scheme(self):
        from web_api.common import npc_leaderboard_key

        assert npc_leaderboard_key(202606, 3129) == "leaderboard:npc:3129:202606"
        assert (
            npc_leaderboard_key(202606, 3129, group_id=42)
            == "leaderboard:group:42:npc:3129:202606"
        )

    def test_resolve_period_forms(self):
        from web_api.common import resolve_period
        from utils.partitions import month_token

        assert resolve_period("202606") == "202606"
        assert resolve_period("2026W27") == "2026W27"
        assert resolve_period("20260617") == "20260617"
        assert resolve_period("all") == "all"
        assert resolve_period("nonsense") == month_token()

    def test_read_write_key_parity(self):
        # The write path (services/redis_updates) and the read path
        # (web_api.common) must agree on the monthly global + group keys.
        from web_api.common import leaderboard_key
        from utils.partitions import month_token

        token = month_token()
        # Mirror services.redis_updates._increment_leaderboards key formatting.
        assert leaderboard_key(token) == f"leaderboard:{token}"
        assert leaderboard_key(token, group_id=5) == f"leaderboard:{token}:group:5"

    def test_problem_shape(self):
        from web_api.common import ProblemException

        e = ProblemException(404, "Not found", "no such thing")
        assert e.status == 404 and e.title == "Not found" and e.detail == "no such thing"


# ── Session tokens (Task 02) ──────────────────────────────────────────────────

class TestSession:
    @pytest.fixture(autouse=True)
    def _redis_denylist_empty(self):
        # The stubbed Redis returns a truthy MagicMock for get(); make the
        # deny-list behave like real Redis (absent jti -> None).
        from web_api import session as sess

        conn = sess._rc()
        if conn is not None:
            conn.get.return_value = None
        yield

    def test_mint_verify_roundtrip(self):
        from web_api.session import mint_session, verify_session

        token = mint_session(4242)
        claims = verify_session(token)
        assert claims is not None
        assert claims["sub"] == 4242
        assert "exp" in claims and "jti" in claims

    def test_invalid_token(self):
        from web_api.session import verify_session

        assert verify_session("not-a-jwt") is None
        assert verify_session("") is None

    def test_expired_token(self):
        from web_api.session import mint_session, verify_session

        token = mint_session(1, ttl=-10)  # already expired
        assert verify_session(token) is None


# ── Guild permission extraction (Task 02 role derivation) ─────────────────────

class TestGuildPerms:
    def test_extract_manageable_guilds(self):
        from web_api.deps import extract_manageable_guilds

        guilds = [
            {"id": "111", "owner": True, "permissions": 0},
            {"id": "222", "owner": False, "permissions": 0x20},   # MANAGE_GUILD
            {"id": "333", "owner": False, "permissions": 0x400},  # not manage
        ]
        assert extract_manageable_guilds(guilds) == {"111", "222"}

    def test_extract_manageable_guild_meta(self):
        from web_api.deps import extract_manageable_guild_meta

        guilds = [
            {"id": "111", "name": "Owned", "icon": "abc", "owner": True, "permissions": 0},
            {"id": "222", "name": "Managed", "icon": None, "owner": False, "permissions": 0x20},
            {"id": "333", "name": "Member", "owner": False, "permissions": 0x400},
            {"id": "111", "name": "Owned dupe", "owner": True, "permissions": 0},
        ]
        meta = extract_manageable_guild_meta(guilds)
        assert meta == [
            {"id": "111", "name": "Owned", "icon": "abc"},
            {"id": "222", "name": "Managed", "icon": None},
        ]

    def test_group_admin_role_predicate(self):
        from web_api.deps import is_group_admin_role

        assert is_group_admin_role("owner")
        assert is_group_admin_role("admin")
        assert not is_group_admin_role("member")
        assert not is_group_admin_role(None)


# ── Request-layer wiring (Tasks 01/02) ────────────────────────────────────────

class TestApp:
    @pytest.fixture()
    def client(self):
        import web_api

        return web_api.create_app().test_client()

    async def test_health(self, client):
        r = await client.get("/api/v1/health")
        assert r.status_code == 200
        assert (await r.get_json())["status"] == "ok"

    async def test_ping(self, client):
        r = await client.get("/api/v1/ping")
        assert r.status_code == 200

    async def test_openapi_served(self, client):
        r = await client.get("/api/v1/openapi.json")
        assert r.status_code == 200
        assert "paths" in (await r.get_json())

    async def test_me_requires_session(self, client):
        r = await client.get("/api/v1/me")
        assert r.status_code == 401
        assert "problem+json" in r.headers.get("content-type", "")

    async def test_settings_requires_session(self, client):
        r = await client.get("/api/v1/me/settings")
        assert r.status_code == 401

    async def test_patch_me_requires_session(self, client):
        r = await client.patch("/api/v1/me", json={"never_ping": True})
        assert r.status_code == 401

    async def test_patch_my_player_requires_session(self, client):
        r = await client.patch("/api/v1/me/players/1", json={"hidden": True})
        assert r.status_code == 401

    async def test_auth_rejects_bad_snowflake(self, client):
        r = await client.post(
            "/api/v1/auth/discord", json={"discord_profile": {"id": "abc"}}
        )
        assert r.status_code == 400

    async def test_unknown_route_is_problem_404(self, client):
        r = await client.get("/api/v1/does-not-exist")
        assert r.status_code == 404
        assert "problem+json" in r.headers.get("content-type", "")

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/groups/1/config"),
            ("patch", "/api/v1/groups/1/config"),
            ("get", "/api/v1/groups/1/pb-bosses"),
            ("post", "/api/v1/submissions/manual"),
            ("get", "/api/v1/uploads/presign"),
            ("get", "/api/v1/groups/1/members"),
            ("patch", "/api/v1/groups/1/hidden-players"),
            ("post", "/api/v1/groups/1/wom-sync"),
            ("get", "/api/v1/groups/1/diagnostics"),
            ("post", "/api/v1/groups"),
            ("get", "/api/v1/groups/guild-status/123456789"),
            ("get", "/api/v1/me/guilds"),
            ("get", "/api/v1/me/players/claim-preview"),
            ("post", "/api/v1/me/players/claim"),
            ("delete", "/api/v1/me/players/1/claim"),
            ("get", "/api/v1/groups/1/subscription"),
            ("post", "/api/v1/groups/1/subscription/checkout"),
            ("post", "/api/v1/groups/1/announcements"),
            ("post", "/api/v1/announcements"),
            ("get", "/api/v1/admin/services"),
            ("post", "/api/v1/admin/discord/send"),
            ("get", "/api/v1/admin/lookup"),
            ("get", "/api/v1/admin/overview"),
            ("get", "/api/v1/admin/data/players"),
            ("get", "/api/v1/admin/data/players/1"),
            ("patch", "/api/v1/admin/data/players/1"),
            ("get", "/api/v1/admin/logs"),
            ("post", "/api/v1/admin/groups/1/subscription/grant"),
            ("post", "/api/v1/admin/groups/1/subscription/revoke"),
            ("get", "/api/v1/admin/groups/1/overview"),
            ("post", "/api/v1/events"),
        ],
    )
    async def test_authed_endpoints_require_session(self, client, method, path):
        r = await getattr(client, method)(path, json={})
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"

    async def test_public_reads_do_not_require_session(self, client):
        # These must not 401 (they may 200/404/502 depending on data/redis).
        for path in ["/api/v1/subscriptions/tiers", "/api/v1/events", "/api/v1/announcements"]:
            r = await client.get(path)
            assert r.status_code != 401, f"{path} unexpectedly gated"

    async def test_bot_invite_is_public(self, client, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_CLIENT_ID", "424242")
        monkeypatch.delenv("DISCORD_BOT_INVITE_PERMISSIONS", raising=False)
        r = await client.get("/api/v1/meta/bot-invite")
        assert r.status_code == 200
        body = await r.get_json()
        assert body["client_id"] == "424242"
        assert body["permissions"] is None
        assert body["invite_url"].startswith("https://discord.com/oauth2/authorize?")
        assert "client_id=424242" in body["invite_url"]


class TestProfileStatHelpers:
    """Pure helpers behind the group/player profile stat blocks."""

    def test_previous_partition_mid_year(self):
        from web_api.routes.profiles import _previous_partition

        assert _previous_partition(202607) == 202606

    def test_previous_partition_january_wraps(self):
        from web_api.routes.profiles import _previous_partition

        assert _previous_partition(202601) == 202512

    def test_convert_from_ms_formats(self):
        from web_api.routes.profiles import _convert_from_ms

        assert _convert_from_ms(58800) == "0:58.8"
        assert _convert_from_ms(872000) == "14:32.0"
        assert _convert_from_ms(3_723_400) == "1:02:03.4"
