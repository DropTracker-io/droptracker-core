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
