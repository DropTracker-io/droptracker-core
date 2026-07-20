"""Unit tests for the pure helpers of services/event_team_discord_bot.py
(channel intro copy + name/color parsing). Loaded by file path — module-level
imports are sqlalchemy/stdlib only, so the conftest stubs never interfere."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]


def _load():
    name = "_event_team_discord_bot_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, _ROOT / "services" / "event_team_discord_bot.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bot_mod = _load()
_team = SimpleNamespace(name="Reds")


def _event(**kw):
    base = {"kind": "standard", "has_bingo": False}
    base.update(kw)
    return SimpleNamespace(**base)


class TestChannelIntro:
    def test_bingo_never_mentions_rolls(self):
        intro = bot_mod._channel_intro(_event(has_bingo=True, kind="bingo"), _team)
        assert "roll" not in intro.lower()
        assert "tile" in intro.lower()

    def test_board_game_mentions_rolls(self):
        intro = bot_mod._channel_intro(_event(kind="board_game"), _team)
        assert "roll" in intro.lower()

    def test_standard_is_generic(self):
        intro = bot_mod._channel_intro(_event(), _team)
        assert "roll" not in intro.lower()
        assert "Reds" in intro


class TestParseColor:
    def test_hex_parses(self):
        assert bot_mod._parse_color("#ff0000") == 0xFF0000

    def test_garbage_none(self):
        assert bot_mod._parse_color("red") is None
        assert bot_mod._parse_color(None) is None


class TestExpectedMemberErrorFilter:
    """The bots/main.py logging filter — re-implemented check against the
    exact record shapes interactions emits (kept in sync by string contract)."""

    def _filter(self):
        import logging

        class F(logging.Filter):
            def filter(self, record):
                try:
                    msg = record.getMessage()
                except Exception:
                    return True
                if "/members/" not in msg and "/thread-members/" not in msg:
                    return True
                stripped = msg.rstrip()
                return not (stripped.endswith(": 403") or stripped.endswith(": 404"))

        return F()

    def _record(self, msg):
        import logging

        return logging.LogRecord("interactions", logging.ERROR, __file__, 1,
                                 msg, None, None)

    def test_member_404_dropped(self):
        f = self._filter()
        assert not f.filter(self._record(
            "GET::https://discord.com/api/v10/guilds/1/members/2: 404"))
        assert not f.filter(self._record(
            "PUT::https://discord.com/api/v10/guilds/1/members/2/roles/3: 403"))
        assert not f.filter(self._record(
            "PUT::https://discord.com/api/v10/channels/9/thread-members/2: 404"))

    def test_other_errors_kept(self):
        f = self._filter()
        assert f.filter(self._record(
            "GET::https://discord.com/api/v10/guilds/1/members/2: 500"))
        assert f.filter(self._record(
            "POST::https://discord.com/api/v10/channels/9/messages: 403"))
        assert f.filter(self._record("some unrelated error"))


class TestSyncMembersOutcomes:
    """The tri-state member sync: applied/absent are 'handled', transient
    errors are retried (never silently marked handled — the 2026-07-17
    outage regression)."""

    def _run(self, outcomes: dict, desired: set, state=None):
        import asyncio
        import json

        ix_errors = _install_fake_interactions_errors()

        class Http:
            async def add_guild_member_role(self, gid, uid, rid, reason=None):
                kind = outcomes.get(str(uid), "ok")
                if kind == "absent":
                    raise ix_errors.NotFound()
                if kind == "forbidden":
                    raise ix_errors.Forbidden()
                if kind == "error":
                    raise RuntimeError("boom")

            async def remove_guild_member_role(self, gid, uid, rid, reason=None):
                return await self.add_guild_member_role(gid, uid, rid, reason)

        class Bot:
            http = Http()

        row = SimpleNamespace(guild_id="1", role_id="2", channel_id=None,
                              channel_kind=None, last_error=None,
                              member_state=json.dumps(sorted(state)) if state else None)
        converged = asyncio.get_event_loop().run_until_complete(
            bot_mod._sync_members(Bot(), None, row, desired))
        return converged, set(json.loads(row.member_state)), row

    def test_ok_and_absent_are_handled(self):
        converged, state, _row = self._run(
            {"10": "ok", "11": "absent"}, {"10", "11"})
        assert converged
        assert state == {"10", "11"}

    def test_forbidden_not_marked_handled(self):
        # Audit P0-11: a 403 (bot role below the team role / Manage Roles
        # revoked) must NOT be treated as "absent" — the member never got the
        # role while the UI read "synced". It stays unhandled (retried once
        # perms are fixed) and leaves an actionable last_error on the row.
        converged, state, row = self._run(
            {"10": "ok", "12": "forbidden"}, {"10", "12"})
        assert not converged
        assert state == {"10"}
        assert row.last_error and "Manage Roles" in row.last_error

    def test_transient_error_not_marked_handled(self):
        converged, state, _row = self._run(
            {"10": "ok", "11": "error"}, {"10", "11"})
        assert not converged  # stays members_dirty — retried next pass
        assert state == {"10"}

    def test_remove_error_keeps_id_for_retry(self):
        converged, state, _row = self._run(
            {"99": "error"}, set(), state={"99"})
        assert not converged
        assert state == {"99"}


def _install_fake_interactions_errors():
    """The conftest stubs `interactions` as a bare MagicMock (not a package),
    so the module's lazy `from interactions.client import errors` can't
    resolve. Register a minimal errors module carrying the two classes the
    sync code catches; the fakes are what the test raises too."""
    import types

    errors = sys.modules.get("interactions.client.errors")
    if errors is None or not isinstance(getattr(errors, "NotFound", None), type):
        errors = types.ModuleType("interactions.client.errors")

        class NotFound(Exception):
            pass

        class Forbidden(Exception):
            pass

        errors.NotFound = NotFound
        errors.Forbidden = Forbidden
        client_pkg = types.ModuleType("interactions.client")
        client_pkg.errors = errors
        sys.modules["interactions.client"] = client_pkg
        sys.modules["interactions.client.errors"] = errors
    return errors
