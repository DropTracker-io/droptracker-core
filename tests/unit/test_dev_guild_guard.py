"""Unit tests for utils.dev_guild_guard.

The guard confines a dev instance's outbound Discord calls to the guilds it
actually serves, so a prod-copy database's ~262 stale guild ids and ~1,600
stale channel ids stop generating ~72k failed 403/404 calls a day.

The two invariants that matter most:

* it is INERT in production — no dev mode, or no allowlist, means every call
  keeps its stock behaviour;
* it never swallows a channel the bot can really see. `ChannelType.GUILD_TEXT`
  is 0, so a truthiness test on `channel.type` would block every ordinary text
  channel; that trap has an explicit regression below.
"""
import asyncio

import pytest

from utils import dev_guild_guard as guard


DEV_GUILD = 1436315863434133598
PROD_GUILD = 1172737525069135962


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("STATE", raising=False)
    monkeypatch.delenv("STATUS", raising=False)
    monkeypatch.delenv(guard.ALLOWLIST_ENV, raising=False)
    guard._local_dead_channels.clear()
    guard._blocked_counts.update({"guild": 0, "channel": 0})
    # Keep the negative cache purely in-process for tests.
    monkeypatch.setattr(guard, "_redis", lambda: None)
    yield
    guard._local_dead_channels.clear()


def _dev(monkeypatch, allowlist=str(DEV_GUILD)):
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.setenv(guard.ALLOWLIST_ENV, allowlist)


def _run(coro):
    return asyncio.run(coro)


class FakeBot:
    """Stands in for the interactions Client. Records what it was asked for."""

    def __init__(self, guild=object(), channel=None, raises=None):
        self.guild_calls = []
        self.channel_calls = []
        self._guild = guild
        self._channel = channel
        self._raises = raises

    async def fetch_guild(self, guild_id, *args, **kwargs):
        self.guild_calls.append(guild_id)
        return self._guild

    async def fetch_channel(self, channel_id, *args, **kwargs):
        self.channel_calls.append(channel_id)
        if self._raises is not None:
            raise self._raises
        return self._channel


class FakeChannel:
    """A channel that really resolved, in `guild_id`."""

    def __init__(self, guild_id, channel_type=0):
        self._guild_id = guild_id
        self.type = channel_type


# --- arming -----------------------------------------------------------------

def test_inactive_without_dev_mode(monkeypatch):
    monkeypatch.setenv(guard.ALLOWLIST_ENV, str(DEV_GUILD))
    assert guard.is_active() is False
    bot = FakeBot()
    assert guard.install(bot) is False


def test_inactive_without_allowlist(monkeypatch):
    monkeypatch.setenv("STATE", "dev")
    assert guard.is_active() is False


def test_empty_allowlist_never_means_block_everything(monkeypatch):
    """A misconfigured allowlist must leave the bot unconfined, not mute it."""
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.setenv(guard.ALLOWLIST_ENV, "   ")
    assert guard.is_active() is False
    bot = FakeBot()
    guard.install(bot)
    assert _run(bot.fetch_guild(PROD_GUILD)) is not None


def test_quoted_env_values_are_understood(monkeypatch):
    # .env in this repo writes STATE="dev" with the quotes included.
    monkeypatch.setenv("STATE", '"dev"')
    monkeypatch.setenv(guard.ALLOWLIST_ENV, str(DEV_GUILD))
    assert guard.is_dev_mode() is True
    assert guard.is_active() is True


def test_status_alone_also_arms(monkeypatch):
    monkeypatch.setenv("STATUS", "dev")
    monkeypatch.setenv(guard.ALLOWLIST_ENV, str(DEV_GUILD))
    assert guard.is_active() is True


def test_install_is_idempotent(monkeypatch):
    _dev(monkeypatch)
    bot = FakeBot()
    assert guard.install(bot) is True
    first = bot.fetch_guild
    assert guard.install(bot) is True
    assert bot.fetch_guild is first


# --- allowlist parsing ------------------------------------------------------

@pytest.mark.parametrize("raw", [
    f"{DEV_GUILD}",
    f" {DEV_GUILD} ",
    f'["{DEV_GUILD}"]',
    f"[{DEV_GUILD}]",
    f"{DEV_GUILD},",
    f"{DEV_GUILD};",
])
def test_allowlist_accepts_json_and_csv(monkeypatch, raw):
    monkeypatch.setenv(guard.ALLOWLIST_ENV, raw)
    assert guard.allowed_guild_ids() == {DEV_GUILD}


def test_allowlist_multiple_ids(monkeypatch):
    monkeypatch.setenv(guard.ALLOWLIST_ENV, f"{DEV_GUILD},{PROD_GUILD}")
    assert guard.allowed_guild_ids() == {DEV_GUILD, PROD_GUILD}


def test_allowlist_ignores_garbage(monkeypatch):
    monkeypatch.setenv(guard.ALLOWLIST_ENV, f"{DEV_GUILD},not-an-id,")
    assert guard.allowed_guild_ids() == {DEV_GUILD}


def test_malformed_json_does_not_arm(monkeypatch):
    monkeypatch.setenv("STATE", "dev")
    monkeypatch.setenv(guard.ALLOWLIST_ENV, "[not json")
    assert guard.allowed_guild_ids() == set()
    assert guard.is_active() is False


# --- guilds -----------------------------------------------------------------

def test_foreign_guild_is_refused_without_a_request(monkeypatch):
    _dev(monkeypatch)
    bot = FakeBot()
    guard.install(bot)
    assert _run(bot.fetch_guild(PROD_GUILD)) is None
    assert bot.guild_calls == []
    assert guard.blocked_counts()["guild"] == 1


def test_allowed_guild_passes_through(monkeypatch):
    _dev(monkeypatch)
    sentinel = object()
    bot = FakeBot(guild=sentinel)
    guard.install(bot)
    assert _run(bot.fetch_guild(DEV_GUILD)) is sentinel
    assert bot.guild_calls == [DEV_GUILD]


def test_string_guild_ids_match(monkeypatch):
    # Group.guild_id is frequently a string in this codebase.
    _dev(monkeypatch)
    bot = FakeBot()
    guard.install(bot)
    _run(bot.fetch_guild(str(DEV_GUILD)))
    assert bot.guild_calls == [str(DEV_GUILD)]


def test_unparseable_guild_id_is_not_swallowed(monkeypatch):
    _dev(monkeypatch)
    bot = FakeBot()
    guard.install(bot)
    _run(bot.fetch_guild("https://discord.com/channels/123/456"))
    # Let the caller see its own malformed data fail rather than hiding it.
    assert bot.guild_calls == ["https://discord.com/channels/123/456"]


# --- channels ---------------------------------------------------------------

def test_real_text_channel_is_never_blocked(monkeypatch):
    """Regression: ChannelType.GUILD_TEXT is 0. A truthiness test on
    `channel.type` would treat every ordinary text channel as inaccessible and
    silently swallow all notifications."""
    _dev(monkeypatch)
    channel = FakeChannel(DEV_GUILD, channel_type=0)
    bot = FakeBot(channel=channel)
    guard.install(bot)
    assert _run(bot.fetch_channel(555)) is channel
    assert guard.is_channel_known_dead(555) is False


def test_channel_in_foreign_guild_is_learned_then_blocked(monkeypatch):
    _dev(monkeypatch)
    bot = FakeBot(channel=FakeChannel(PROD_GUILD))
    guard.install(bot)
    # First call costs one request — a channel id carries no guild, so it can
    # only be attributed once resolved.
    assert _run(bot.fetch_channel(777)) is None
    assert bot.channel_calls == [777]
    # Every call after that is free.
    assert _run(bot.fetch_channel(777)) is None
    assert bot.channel_calls == [777]
    assert guard.blocked_counts()["channel"] == 1


def test_forbidden_stub_is_learned(monkeypatch):
    """The 403 path returns a bare BaseChannel that the library never caches,
    which is exactly why the same channel was re-fetched every cycle.

    conftest stubs `interactions` as a MagicMock, so the real class cannot be
    imported here; the identity rule is what is under test, and the real
    library is checked by tests/manual parity below.
    """
    class FakeBaseChannel:
        pass

    class FakeGuildText(FakeBaseChannel):
        _guild_id = DEV_GUILD
        type = 0

    monkeypatch.setattr(guard, "_BaseChannel", FakeBaseChannel)
    _dev(monkeypatch)

    stub = FakeBaseChannel()
    bot = FakeBot(channel=stub)
    guard.install(bot)
    assert _run(bot.fetch_channel(888)) is None
    assert guard.is_channel_known_dead(888) is True
    assert _run(bot.fetch_channel(888)) is None
    assert bot.channel_calls == [888]

    # A subclass is a channel that really resolved and must pass through.
    real = FakeGuildText()
    bot2 = FakeBot(channel=real)
    bot2._dev_guild_guard_installed = False
    guard.install(bot2)
    assert _run(bot2.fetch_channel(889)) is real


def test_none_channel_is_learned(monkeypatch):
    _dev(monkeypatch)
    bot = FakeBot(channel=None)
    guard.install(bot)
    assert _run(bot.fetch_channel(999)) is None
    assert guard.is_channel_known_dead(999) is True


@pytest.mark.parametrize("status", [403, 404])
def test_http_errors_are_learned_and_still_raised(monkeypatch, status):
    _dev(monkeypatch)

    class HTTPErr(Exception):
        def __init__(self):
            self.status = status

    bot = FakeBot(raises=HTTPErr())
    guard.install(bot)
    with pytest.raises(HTTPErr):
        _run(bot.fetch_channel(1234))
    assert guard.is_channel_known_dead(1234) is True


def test_unrelated_error_is_not_learned(monkeypatch):
    """A 5xx or a network blip must not mark a live channel dead."""
    _dev(monkeypatch)

    class ServerErr(Exception):
        status = 503

    bot = FakeBot(raises=ServerErr())
    guard.install(bot)
    with pytest.raises(ServerErr):
        _run(bot.fetch_channel(4321))
    assert guard.is_channel_known_dead(4321) is False


def test_dm_channel_without_guild_is_allowed(monkeypatch):
    # No guild to attribute it to, so it must pass through untouched.
    _dev(monkeypatch)

    class DM:
        type = 1

    dm = DM()
    bot = FakeBot(channel=dm)
    guard.install(bot)
    assert _run(bot.fetch_channel(11)) is dm


def test_dead_marker_expires(monkeypatch):
    _dev(monkeypatch)
    guard.mark_channel_dead(31337)
    assert guard.is_channel_known_dead(31337) is True
    guard._local_dead_channels["31337"] = 0  # simulate TTL elapsing
    assert guard.is_channel_known_dead(31337) is False


def test_redis_failure_degrades_to_local_cache(monkeypatch):
    _dev(monkeypatch)

    class BrokenRedis:
        def get(self, key):
            raise ConnectionError("redis down")

        def setex(self, key, ttl, value):
            raise ConnectionError("redis down")

    monkeypatch.setattr(guard, "_redis", lambda: BrokenRedis())
    bot = FakeBot(channel=FakeChannel(PROD_GUILD))
    guard.install(bot)
    assert _run(bot.fetch_channel(2222)) is None
    assert _run(bot.fetch_channel(2222)) is None
    assert bot.channel_calls == [2222]


def test_describe_reports_state(monkeypatch):
    assert "inactive" in guard.describe()
    monkeypatch.setenv("STATE", "dev")
    assert "unconfined" in guard.describe()
    monkeypatch.setenv(guard.ALLOWLIST_ENV, str(DEV_GUILD))
    assert "active" in guard.describe()
