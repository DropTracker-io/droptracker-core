"""Pure-logic tests for services/clan_chat_bridge.py — sanitizers and the
per-channel batching step.

Loaded directly from the file path (like test_plugin_notifications.py)
because the conftest stubs the ``services`` package; redis/db/discord imports
are lazy inside the functions under test, so the pure paths never touch them.
"""

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load("_clan_chat_bridge_under_test", "services", "clan_chat_bridge.py")


# ── game → Discord sanitization ─────────────────────────────────────────────

def test_game_line_strips_client_markup_and_escapes_markdown():
    assert bridge.sanitize_game_line("<img=41>hello *world*") == r"hello \*world\*"
    assert bridge.sanitize_game_line("<col=ff0000>red text</col>") == "red text"


def test_game_line_neutralizes_mass_mentions():
    out = bridge.sanitize_game_line("@everyone free stuff @here")
    assert "@everyone" not in out  # zero-width space injected
    assert "@here" not in out
    assert "everyone" in out


def test_game_line_folds_nbsp_and_whitespace():
    assert bridge.sanitize_game_line("Iron Botanist   says  hi") == "Iron Botanist says hi"
    assert bridge.sanitize_game_line(None) == ""


def test_escape_markdown_covers_the_usual_suspects():
    assert bridge.escape_markdown("_a_ *b* ~c~ `d` |e| >f") == r"\_a\_ \*b\* \~c\~ \`d\` \|e\| \>f"


# ── Discord → game sanitization ─────────────────────────────────────────────

def test_discord_content_collapses_custom_emoji_and_mentions():
    out = bridge.sanitize_discord_content("<:kekw:1234567> hi <@999> in <#123> <@&55>")
    assert out == ":kekw: hi @user in #channel @role"


def test_discord_content_animated_emoji_and_newlines():
    out = bridge.sanitize_discord_content("<a:party:42>\ngz\ngz again")
    assert out == ":party: | gz | gz again"


def test_discord_content_is_length_capped():
    out = bridge.sanitize_discord_content("x" * 1000)
    assert len(out) == bridge.DISCORD_TO_GAME_MAX_CHARS
    assert out.endswith("…")


def test_discord_content_empty_and_none():
    assert bridge.sanitize_discord_content("") == ""
    assert bridge.sanitize_discord_content(None) == ""
    assert bridge.sanitize_discord_content("   ") == ""


# ── per-channel batching ────────────────────────────────────────────────────

def test_batch_lines_groups_by_channel_and_renders():
    entries = [
        {"channel_id": "111", "sender": "Alice", "message": "hi"},
        {"channel_id": "222", "sender": "Bob", "message": "yo"},
        {"channel_id": "111", "sender": "Carol", "message": "gz *big*"},
    ]
    batches = bridge.batch_lines_by_channel(entries)
    assert set(batches) == {"111", "222"}
    assert batches["111"] == ["**Alice**: hi", r"**Carol**: gz \*big\*"]
    assert batches["222"] == ["**Bob**: yo"]


def test_batch_lines_drops_incomplete_entries():
    entries = [
        {"channel_id": "", "sender": "Alice", "message": "hi"},
        {"channel_id": "111", "sender": "", "message": "hi"},
        {"channel_id": "111", "sender": "Alice", "message": ""},
        {"channel_id": "111", "sender": "<img=1>", "message": "<col=f>"},
    ]
    assert bridge.batch_lines_by_channel(entries) == {}


def test_mirror_message_cap_constant_is_under_discord_limit():
    assert bridge.MIRROR_MESSAGE_MAX_CHARS <= 2000
