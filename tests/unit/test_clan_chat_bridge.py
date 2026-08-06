"""Pure-logic tests for services/clan_chat_bridge.py — sanitizers and the
per-channel batching step.

Loaded directly from the file path (like test_plugin_notifications.py)
because the conftest stubs the ``services`` package; redis/db/discord imports
are lazy inside the functions under test, so the pure paths never touch them.
"""

import importlib.util
import json
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


# ── broadcast lines (no speaker) ────────────────────────────────────────────

def test_batch_renders_broadcasts_without_a_sender():
    entries = [
        {"channel_id": "111", "kind": "broadcast",
         "message": "Alice received a drop: Twisted bow (1,000,000 coins)."},
        {"channel_id": "111", "kind": "chat", "sender": "Bob", "message": "gz"},
    ]
    batches = bridge.batch_lines_by_channel(entries)
    assert batches["111"] == [
        "📢 *Alice received a drop: Twisted bow (1,000,000 coins).*",
        "**Bob**: gz",
    ]


def test_broadcast_message_is_still_markdown_escaped():
    entries = [{"channel_id": "111", "kind": "broadcast", "message": "a *b* _c_"}]
    assert bridge.batch_lines_by_channel(entries)["111"] == [r"📢 *a \*b\* \_c\_*"]


def test_broadcast_entry_still_needs_a_message():
    entries = [
        {"channel_id": "111", "kind": "broadcast", "message": ""},
        {"channel_id": "", "kind": "broadcast", "message": "hi"},
    ]
    assert bridge.batch_lines_by_channel(entries) == {}


def test_entries_without_a_kind_read_as_chat():
    """Lines staged before broadcasts were mirrored are still in Redis."""
    entries = [{"channel_id": "111", "sender": "Alice", "message": "hi"}]
    assert bridge.batch_lines_by_channel(entries) == {"111": ["**Alice**: hi"]}


# ── broadcast mirroring (bound groups + per-group first-sight claim) ─────────

class _FakeRedis:
    """Just enough for the SET NX claims and the staging pipeline."""

    def __init__(self):
        self.keys = {}
        self.pushed = []
        self.counters = {}

    def set(self, key, _value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = ex
        return True

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, _ttl):
        return True

    def pipeline(self):
        return self

    def rpush(self, _key, value):
        self.pushed.append(value)

    def ltrim(self, *_a):
        return True

    def execute(self):
        return [None]


def _mirror_env(monkeypatch, bound):
    fake = _FakeRedis()
    monkeypatch.setattr(bridge, "_redis", lambda: fake)
    monkeypatch.setattr(bridge, "bridge_bound_groups", lambda *_a, **_k: bound)
    return fake


def test_broadcast_mirrors_once_per_bridged_group(monkeypatch):
    fake = _mirror_env(monkeypatch, {10: "111", 11: "222"})
    line = "Alice received a drop: Twisted bow."

    assert bridge.mirror_broadcast_line(None, 42, "my-clan", line) == 2
    entries = [json.loads(p) for p in fake.pushed]
    assert [(e["group_id"], e["channel_id"], e["kind"]) for e in entries] == [
        (10, "111", "broadcast"), (11, "222", "broadcast")
    ]
    assert all(e["message"] == line for e in entries)
    # A second relayer's copy of the same line is collapsed, per group.
    assert bridge.mirror_broadcast_line(None, 99, "my-clan", line) == 0
    assert len(fake.pushed) == 2


def test_broadcast_mirror_claim_is_per_group_not_per_clan(monkeypatch):
    """Two groups bridging one clan through different relayers both get it."""
    fake = _mirror_env(monkeypatch, {10: "111"})
    line = "Bob received a drop: Scythe of vitur."
    assert bridge.mirror_broadcast_line(None, 42, "my-clan", line) == 1

    monkeypatch.setattr(bridge, "bridge_bound_groups", lambda *_a, **_k: {11: "222"})
    assert bridge.mirror_broadcast_line(None, 99, "my-clan", line) == 1
    assert len(fake.pushed) == 2


def test_broadcast_mirror_no_bridge_stages_nothing(monkeypatch):
    fake = _mirror_env(monkeypatch, {})
    assert bridge.mirror_broadcast_line(None, 42, "my-clan", "anything") == 0
    assert fake.pushed == []


def test_broadcast_mirror_ignores_blank_line_and_clan(monkeypatch):
    fake = _mirror_env(monkeypatch, {10: "111"})
    assert bridge.mirror_broadcast_line(None, 42, "my-clan", "   ") == 0
    assert bridge.mirror_broadcast_line(None, 42, "", "a real line") == 0
    assert fake.pushed == []


# ── shared bridge rate budget ───────────────────────────────────────────────

def test_rate_limit_is_shared_and_capped(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(bridge, "_redis", lambda: fake)
    for _ in range(bridge.BRIDGE_RATE_LIMIT_PER_MIN):
        assert bridge.relayer_within_rate_limit(42) is True
    assert bridge.relayer_within_rate_limit(42) is False
    # Per relayer, not global.
    assert bridge.relayer_within_rate_limit(43) is True


def test_rate_limit_fails_open_when_redis_is_down(monkeypatch):
    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(bridge, "_redis", boom)
    assert bridge.relayer_within_rate_limit(42) is True
    assert bridge._claim_first_sight("k", 60) is True
