"""MIRROR_SINK_GROUP_ID validation (workers/webhook_consumer.py).

This is the guard between "mirror production traffic at dev" and "post
production traffic into a real clan's Discord". A mistyped group id is the whole
risk, so every path that cannot *positively confirm* the sink lives in a
dev-allowlisted guild has to refuse — "cannot tell" and "not safe" must mean the
same thing here.
"""

import pytest

wc = pytest.importorskip("workers.webhook_consumer")


class _FakeSession:
    """Stands in for the group lookup. `scalar()` is the guild_id, or None."""

    def __init__(self, guild_id):
        self._guild_id = guild_id
        self.closed = False

    def query(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def scalar(self):
        return self._guild_id

    def close(self):
        self.closed = True


@pytest.fixture()
def dev_env(monkeypatch):
    """A correctly configured dev instance; individual tests break one thing."""
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.setenv("DEV_ALLOWED_GUILDS", "1436315863434133598")
    monkeypatch.setenv("MIRROR_SINK_GROUP_ID", "42")
    return monkeypatch


def _with_guild(monkeypatch, guild_id):
    """Point the validator's group lookup at a fake session.

    Patched on the module object rather than via `api.core` attribute access:
    `import api.core` binds only `api` locally, and this repo's `api` package is
    already in sys.modules without the submodule resolved as an attribute.
    """
    import importlib

    session = _FakeSession(guild_id)
    monkeypatch.setattr(
        importlib.import_module("api.core"), "get_db_session", lambda: session
    )
    return session


class TestRefuses:
    def test_when_not_a_dev_instance(self, dev_env):
        dev_env.setenv("STATE", "live")
        sink, reason = wc._validate_mirror_sink()
        assert sink is None
        assert "dev instance" in reason

    def test_when_the_sink_is_unset(self, dev_env):
        dev_env.delenv("MIRROR_SINK_GROUP_ID", raising=False)
        sink, reason = wc._validate_mirror_sink()
        assert sink is None
        assert "unset" in reason

    def test_when_the_sink_is_not_a_number(self, dev_env):
        dev_env.setenv("MIRROR_SINK_GROUP_ID", "the-dev-group")
        sink, reason = wc._validate_mirror_sink()
        assert sink is None
        assert "not an integer" in reason

    def test_when_no_allowlist_is_configured(self, dev_env):
        """An empty DEV_ALLOWED_GUILDS means "not configured", so nothing can be
        confirmed safe — it must not be read as "everything is fine"."""
        dev_env.delenv("DEV_ALLOWED_GUILDS", raising=False)
        sink, reason = wc._validate_mirror_sink()
        assert sink is None
        assert "DEV_ALLOWED_GUILDS" in reason

    def test_when_the_group_does_not_exist(self, dev_env):
        _with_guild(dev_env, None)
        sink, reason = wc._validate_mirror_sink()
        assert sink is None
        assert "does not exist" in reason

    def test_when_the_group_is_in_a_real_guild(self, dev_env):
        """The mistyped-id case, and the only one that could do real damage."""
        _with_guild(dev_env, 597397938989432842)
        sink, reason = wc._validate_mirror_sink()
        assert sink is None
        assert "real clan" in reason


class TestAccepts:
    def test_a_group_in_an_allowlisted_guild(self, dev_env):
        _with_guild(dev_env, 1436315863434133598)
        sink, reason = wc._validate_mirror_sink()
        assert sink == 42
        assert reason is None

    def test_quoted_env_values_are_tolerated(self, dev_env):
        """`.env` in this repo is frequently written KEY="value"."""
        dev_env.setenv("MIRROR_SINK_GROUP_ID", '"42"')
        _with_guild(dev_env, 1436315863434133598)
        sink, _ = wc._validate_mirror_sink()
        assert sink == 42

    def test_the_session_is_closed(self, dev_env):
        session = _with_guild(dev_env, 1436315863434133598)
        wc._validate_mirror_sink()
        assert session.closed, "the validator must not leak a DB session"


class TestResolveIsMemoisedAndSafe:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        wc._mirror_sink_cache.update({"resolved": False, "value": None})
        yield
        wc._mirror_sink_cache.update({"resolved": False, "value": None})

    def test_resolves_once(self, dev_env):
        calls = {"n": 0}
        real = wc._validate_mirror_sink

        def counting():
            calls["n"] += 1
            return real()

        dev_env.setattr(wc, "_validate_mirror_sink", counting)
        _with_guild(dev_env, 1436315863434133598)

        assert wc._resolve_mirror_sink() == 42
        assert wc._resolve_mirror_sink() == 42
        assert calls["n"] == 1, "one DB query per process, not per submission"

    def test_a_raising_validator_refuses_rather_than_propagating(self, dev_env):
        def boom():
            raise RuntimeError("database is on fire")

        dev_env.setattr(wc, "_validate_mirror_sink", boom)
        assert wc._resolve_mirror_sink() is None
