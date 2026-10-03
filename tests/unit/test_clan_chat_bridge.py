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
        # No staged kind (an entry from before icons) keeps the generic
        # prefix; broadcasts are plain text now, not italics.
        "📢 Alice received a drop: Twisted bow (1,000,000 coins).",
        "**Bob**: gz",
    ]


def test_broadcast_message_is_still_markdown_escaped():
    entries = [{"channel_id": "111", "kind": "broadcast", "message": "a *b* _c_"}]
    assert bridge.batch_lines_by_channel(entries)["111"] == [r"📢 a \*b\* \_c\_"]


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


# ── rank emoji ──────────────────────────────────────────────────────────────

_RANKS = {"deputy_owner": "<:rank_deputy_owner:123>", "recruit": "<:rank_recruit:456>"}


def test_rank_renders_as_a_leading_emoji():
    entries = [
        {"channel_id": "111", "sender": "Alice", "message": "hi", "rank": "deputy_owner"},
        {"channel_id": "111", "sender": "Bob", "message": "yo", "rank": "Recruit"},
    ]
    assert bridge.batch_lines_by_channel(entries, _RANKS)["111"] == [
        "<:rank_deputy_owner:123> **Alice**: hi",
        "<:rank_recruit:456> **Bob**: yo",
    ]


def test_emoji_token_is_not_markdown_escaped():
    """escape_markdown() escapes underscores — routing the token through it
    would produce ``<:rank\\_deputy\\_owner:123>`` and render as literal text."""
    line = bridge.batch_lines_by_channel(
        [{"channel_id": "111", "sender": "Alice", "message": "hi", "rank": "deputy_owner"}],
        _RANKS,
    )["111"][0]
    assert "\\_" not in line
    assert line.startswith("<:rank_deputy_owner:123> ")


def test_unranked_and_unknown_ranks_render_plain_lines():
    entries = [
        {"channel_id": "111", "sender": "Alice", "message": "hi", "rank": None},
        {"channel_id": "111", "sender": "Bob", "message": "yo", "rank": "Not Ranked"},
        {"channel_id": "111", "sender": "Carol", "message": "sup", "rank": "member"},
    ]
    assert bridge.batch_lines_by_channel(entries, _RANKS)["111"] == [
        "**Alice**: hi", "**Bob**: yo", "**Carol**: sup",
    ]


def test_broadcasts_never_take_a_rank_emoji():
    """A staged rank on a system line is meaningless — broadcasts have no
    speaker, so the line keeps its own prefix."""
    entries = [{"channel_id": "111", "kind": "broadcast", "message": "Alice got a pet",
                "rank": "deputy_owner"}]
    assert bridge.batch_lines_by_channel(entries, _RANKS)["111"] == [
        "📢 Alice got a pet"
    ]


def test_rank_emoji_still_fits_the_message_cap():
    """Each token adds ~40 chars to a line; the drain loop splits on the cap,
    so a single ranked line must stay far below it."""
    longest = max(_RANKS.values(), key=len)
    line = f"{longest} **{'x' * 32}**: {'y' * 200}"
    assert len(line) < bridge.MIRROR_MESSAGE_MAX_CHARS


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

    def eval(self, _script, _numkeys, key, relayer, _ttl):
        """The per-relayer occurrence claim (clan_chat_bridge._CLAIM_LINE_LUA)."""
        counts = self.keys.setdefault(key, {})
        counts[relayer] = counts.get(relayer, 0) + 1
        if counts[relayer] > counts.get("_shown", 0):
            counts["_shown"] = counts[relayer]
            return 1
        return 0

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
    assert bridge.claim_relayed_line("k", 42, 60) is True


# ── loop safety: our own Discord render coming back ─────────────────────────

def test_bridge_echo_is_recognized_by_its_marker():
    """The plugin renders a Discord line through client.addChatMessage, which
    posts a real ChatMessage — so a build without the client-side guard relays
    the line straight back to us, wearing the rendered sender."""
    assert bridge.is_bridge_echo("Bob (Discord)") is True
    assert bridge.is_bridge_echo("Bob (Discord)".upper()) is False  # marker is literal


def test_bridge_echo_survives_a_truncated_sender():
    """A 32-char Discord display name pushes the marker past the intake's
    sender cap, so the test is a substring, not a suffix."""
    long_name = "x" * 32 + " (Discord) trailing"
    assert bridge.is_bridge_echo(long_name) is True


def test_bridge_echo_never_fires_on_a_real_clanmate():
    # OSRS display names are letters, digits, spaces, hyphens and underscores —
    # a parenthesis cannot appear in one.
    for name in ("Iron Botanist", "Discord", "Disc0rd", "Beast_Owned", "", None):
        assert bridge.is_bridge_echo(name) is False


# ── per-relayer dedupe: never second-guess a single client ──────────────────

def test_same_relayer_repeating_a_line_mirrors_every_copy(monkeypatch):
    """"gz" twice from one player is two real lines — every relayer sees both,
    so two copies from one relayer can never be a multi-relayer duplicate."""
    fake = _FakeRedis()
    monkeypatch.setattr(bridge, "_redis", lambda: fake)
    assert bridge.claim_relayed_line("k", 42, 60) is True
    assert bridge.claim_relayed_line("k", 42, 60) is True
    assert bridge.claim_relayed_line("k", 42, 60) is True


def test_second_relayer_copies_collapse_until_they_outnumber_what_was_shown(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(bridge, "_redis", lambda: fake)
    # Player says "gz" twice; relayers A (42) and B (99) each relay both,
    # interleaved in whatever order the network delivers them.
    results = [
        bridge.claim_relayed_line("k", 99, 60),  # B copy 1 → shown (first sight)
        bridge.claim_relayed_line("k", 42, 60),  # A copy 1 → duplicate
        bridge.claim_relayed_line("k", 42, 60),  # A copy 2 → second real line
        bridge.claim_relayed_line("k", 99, 60),  # B copy 2 → duplicate
    ]
    assert results == [True, False, True, False]


def test_repeated_broadcast_from_one_relayer_mirrors_twice(monkeypatch):
    """Two identical drop lines inside the window (two kills, same item) from
    the same client are two events, not a relay duplicate."""
    fake = _mirror_env(monkeypatch, {10: "111"})
    line = "Alice received a drop: Tormented synapse."
    assert bridge.mirror_broadcast_line(None, 42, "my-clan", line) == 1
    assert bridge.mirror_broadcast_line(None, 42, "my-clan", line) == 1
    assert bridge.mirror_broadcast_line(None, 99, "my-clan", line) == 0
    assert len(fake.pushed) == 2


# ── broadcast icons + plain text ────────────────────────────────────────────

def _hints(line):
    from utils.clan_broadcasts import parse_broadcast

    return bridge.broadcast_hints(parse_broadcast(line), line)


def test_hints_carry_the_kind_and_the_glyph_source():
    drop = _hints("Loggy received special loot from a raid: Twisted bow.")
    assert drop["bkind"] == "raid_drop" and drop["item"] == "Twisted bow"
    assert drop["subject"] == "Loggy"
    pb = _hints("Sir Vincelot has achieved a new Maggot King personal best: 3:46")
    assert pb["bkind"] == "personal_best" and pb["npc"] == "Maggot King"
    assert _hints("Ann has completed a quest: Dragon Slayer II.")["bkind"] == "quest"
    assert _hints("Ann has completed a hard combat task: Perfect Zulrah.")["bkind"] == (
        "combat_achievement"
    )
    assert _hints("Ann has reached Attack level 88.")["bkind"] == "level_up"


def test_display_only_shapes_get_icons_without_touching_the_parser():
    """ToA/delve PBs, CA tier unlocks and PvP lines are unparsed for tracking
    (a parser match would change what gets recorded) but still get icons."""
    from utils.clan_broadcasts import parse_broadcast

    toa = ("Taka-ta achieved a new Tombs of Amascut (team size: 3) Expert mode "
           "Overall personal best: 25:40.20")
    assert parse_broadcast(toa) is None
    assert bridge.broadcast_hints(None, toa)["bkind"] == "personal_best"
    pvp = ("Godest has been defeated by AlwaysDaGoat in The Wilderness and lost "
           "(844,540 coins) worth of loot.")
    assert bridge.broadcast_hints(parse_broadcast(pvp), pvp)["bkind"] == "pk"
    unlock = "Ann has unlocked the Hard tier of rewards from Combat Achievements!"
    assert bridge.broadcast_hints(None, unlock)["bkind"] == "combat_achievement"
    assert bridge.broadcast_hints(None, "Brand new Jagex wording")["bkind"] is None


def test_each_kind_gets_its_own_icon(monkeypatch):
    from utils import app_emojis, game_emojis

    monkeypatch.setattr(app_emojis, "emoji", lambda key, profile=None: f"<{key}>")
    monkeypatch.setattr(game_emojis, "emoji_for_item",
                        lambda name, profile=None: "<:item_tbow:1>" if name == "Twisted bow" else None)
    monkeypatch.setattr(game_emojis, "emoji_for_npc",
                        lambda name, profile=None: "<:npc_zulrah:2>" if name == "Zulrah" else None)
    icon = bridge.broadcast_icon
    assert icon({"bkind": "raid_drop", "item": "Twisted bow"}) == "<:item_tbow:1>"
    assert icon({"bkind": "item_drop", "item": "Unseeded thing"}) == "💰"
    assert icon({"bkind": "pet", "item": "Unseeded pet"}) == "🐾"
    assert icon({"bkind": "personal_best", "npc": "Zulrah"}) == "<:npc_zulrah:2>"
    assert icon({"bkind": "personal_best", "npc": "Somewhere"}) == "⏱️"
    assert icon({"bkind": "combat_achievement"}) == "<combat_achievement>"
    assert icon({"bkind": "quest"}) == "<quest>"
    assert icon({"bkind": "collection_log"}) == "<collection_log>"
    assert icon({"bkind": "diary"}) == "<diary>"
    assert icon({"bkind": "level_up"}) == "<stats>"
    assert icon({"bkind": "pk"}) == "<skull>"
    assert icon({"bkind": None}) == bridge.BROADCAST_PREFIX


def test_icon_kinds_resolve_to_registered_app_emojis():
    from utils.app_emojis import SPECS

    assert set(bridge._KIND_APP_EMOJI.values()) <= set(SPECS)


def test_broadcast_renders_with_its_icon_in_plain_text(monkeypatch):
    monkeypatch.setattr(bridge, "broadcast_icon", lambda entry: "<quest>")
    entries = [{"channel_id": "111", "kind": "broadcast", "bkind": "quest",
                "message": "Ann has completed a quest: Dragon Slayer II."}]
    assert bridge.batch_lines_by_channel(entries)["111"] == [
        "<quest> Ann has completed a quest: Dragon Slayer II."
    ]


# ── account-type badges ─────────────────────────────────────────────────────

_BADGES = {"ironman": "<:ironman:7>", "group_ironman": "<:group_ironman:8>"}


def _seed_badges(monkeypatch):
    from utils import app_emojis

    monkeypatch.setattr(app_emojis, "seeded_emoji",
                        lambda key, profile=None: _BADGES.get(key))


def test_chat_line_shows_rank_then_badge_then_name(monkeypatch):
    _seed_badges(monkeypatch)
    entries = [{"channel_id": "111", "sender": "Alice", "message": "hi",
                "rank": "deputy_owner", "account_type": "ironman"}]
    assert bridge.batch_lines_by_channel(entries, _RANKS)["111"] == [
        "<:rank_deputy_owner:123> <:ironman:7> **Alice**: hi"
    ]


def test_badge_without_rank_and_normal_accounts_without_badge(monkeypatch):
    _seed_badges(monkeypatch)
    entries = [
        {"channel_id": "111", "sender": "Gim", "message": "yo", "account_type": "group_ironman"},
        {"channel_id": "111", "sender": "Main", "message": "yo", "account_type": "normal"},
        {"channel_id": "111", "sender": "Hc", "message": "yo", "account_type": "hardcore_ironman"},
    ]
    # hardcore_ironman is not "seeded" here → no badge rather than a stand-in.
    assert bridge.batch_lines_by_channel(entries, _RANKS)["111"] == [
        "<:group_ironman:8> **Gim**: yo", "**Main**: yo", "**Hc**: yo",
    ]


def test_broadcast_badge_sits_before_the_subject_name(monkeypatch):
    _seed_badges(monkeypatch)
    monkeypatch.setattr(bridge, "broadcast_icon", lambda entry: "💰")
    entries = [
        {"channel_id": "111", "kind": "broadcast", "subject": "Mistyirons",
         "account_type": "ironman",
         "message": "Mistyirons received special loot from a raid: Elder maul."},
        # Subject not at the start of the line (expelled-style) → no badge.
        {"channel_id": "111", "kind": "broadcast", "subject": "Bob",
         "account_type": "ironman", "message": "Mod has expelled Bob from the clan."},
    ]
    assert bridge.batch_lines_by_channel(entries)["111"] == [
        "💰 <:ironman:7> Mistyirons received special loot from a raid: Elder maul.",
        "💰 Mod has expelled Bob from the clan.",
    ]


def test_badge_keys_are_registered_for_every_game_mode():
    from utils.account_types import ACCOUNT_TYPES_BY_VARBIT
    from utils.app_emojis import SPECS

    assert set(ACCOUNT_TYPES_BY_VARBIT) - {"normal"} <= set(SPECS)
