"""Who may sign in to the dev instance's website.

The dev site runs on a copy of production for testing. Signing in there is
limited to staff and current Bug Testers unless DEV_SITE_LOGIN=open, and a
refused visitor must not leave a users row behind. Production is untouched.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

import web_api.routes.auth as auth
from utils import dev_guild_guard as guard

TESTER, STAFF, STRANGER = "111111111111111111", "222222222222222222", "333333333333333333"


class _Rows:
    def __init__(self, row):
        self.row = row

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self.row


@contextmanager
def _no_row_session():
    yield _Rows(None)


@pytest.fixture()
def dev(monkeypatch):
    """A dev instance where TESTER is a current Bug Tester and nobody is staff."""
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.delenv("DEV_SITE_LOGIN", raising=False)
    monkeypatch.delenv("DEV_ALLOWED_USERS", raising=False)
    monkeypatch.setattr(guard, "tester_user_ids", lambda: {int(TESTER)})
    monkeypatch.setattr(auth, "_SUPERADMIN_DISCORD_IDS", set())
    monkeypatch.setattr(auth, "db_session", _no_row_session)
    return monkeypatch


class TestRefusal:
    def test_production_refuses_nobody(self, monkeypatch):
        monkeypatch.setenv("STATE", "live")
        monkeypatch.delenv("STATUS", raising=False)
        assert auth.dev_login_refusal(STRANGER) is None

    def test_a_tester_may_sign_in(self, dev):
        assert auth.dev_login_refusal(TESTER) is None

    def test_a_stranger_may_not(self, dev):
        assert auth.dev_login_refusal(STRANGER) == "not_a_tester"

    def test_the_explicit_allowlist_may(self, dev):
        dev.setenv("DEV_ALLOWED_USERS", STRANGER)
        assert auth.dev_login_refusal(STRANGER) is None

    def test_a_bootstrap_superadmin_may(self, dev, monkeypatch):
        monkeypatch.setattr(auth, "_SUPERADMIN_DISCORD_IDS", {STRANGER})
        assert auth.dev_login_refusal(STRANGER) is None

    def test_staff_already_on_the_instance_may(self, dev, monkeypatch):
        @contextmanager
        def developer_row():
            yield _Rows((False, True))

        monkeypatch.setattr(auth, "db_session", developer_row)
        assert auth.dev_login_refusal(STAFF) is None

    def test_open_mode_lets_anyone_in(self, dev):
        dev.setenv("DEV_SITE_LOGIN", "open")
        assert auth.dev_login_refusal(STRANGER) is None


class TestRoute:
    @pytest.fixture()
    def client(self):
        import web_api

        return web_api.create_app().test_client()

    async def test_a_stranger_is_refused_before_any_row_is_made(self, client, dev, monkeypatch):
        monkeypatch.setattr(auth, "_find_or_create_user",
                            lambda *a: pytest.fail("no users row for a refused visitor"))
        r = await client.post("/api/v1/auth/discord",
                              json={"discord_profile": {"id": STRANGER, "username": "x"}})
        assert r.status_code == 403
        body = await r.get_json()
        assert body.get("code") == "dev_access_denied"

    async def test_a_tester_gets_a_session(self, client, dev, monkeypatch):
        monkeypatch.setattr(auth, "_find_or_create_user", lambda *a: 42)
        monkeypatch.setattr(auth, "cache_profile", lambda *a, **k: None)
        monkeypatch.setattr(auth, "mint_session", lambda uid: f"token-for-{uid}")
        r = await client.post("/api/v1/auth/discord",
                              json={"discord_profile": {"id": TESTER, "username": "t"}})
        assert r.status_code == 200
        assert (await r.get_json())["session_token"] == "token-for-42"
