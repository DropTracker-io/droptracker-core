"""Pure-logic tests for utils/rank_emojis.py and utils/clan_ranks.py.

The point of the normalization is that three sources spell the same rank three
ways — WOM's ``deputy_owner``, the game's ``"Deputy Owner"`` and the wiki file's
``Deputy_owner`` — and all three have to reach one emoji.
"""

import json

import pytest

from utils import clan_ranks, rank_emojis


# ── normalization ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("spelling", ["deputy_owner", "Deputy Owner", "Deputy_owner",
                                      "  DEPUTY-OWNER  ", "deputy owner"])
def test_every_source_spelling_lands_on_one_key(spelling):
    assert rank_emojis.normalize_rank(spelling) == "deputy_owner"


def test_hyphen_and_space_ranks_agree():
    """The wiki writes Record-chaser / Speed-Runner; WOM writes them with
    underscores. Both must be the same rank."""
    assert rank_emojis.normalize_rank("Record-chaser") == rank_emojis.normalize_rank("record_chaser")
    assert rank_emojis.normalize_rank("Speed-Runner") == rank_emojis.normalize_rank("speed_runner")


def test_normalize_rank_handles_empty_input():
    assert rank_emojis.normalize_rank(None) == ""
    assert rank_emojis.normalize_rank("") == ""
    assert rank_emojis.normalize_rank("   ") == ""
    assert rank_emojis.normalize_rank("---") == ""


def test_emoji_names_are_legal_discord_names():
    """Discord app emoji names are [a-zA-Z0-9_]{2,32}."""
    for rank in ["Deputy Owner", "Short Green Guy", "Record-chaser", "Zenyte"]:
        name = rank_emojis.emoji_name(rank)
        assert name.startswith("rank_")
        assert 2 <= len(name) <= 32
        assert all(c.isalnum() or c == "_" for c in name)


# ── lookup ──────────────────────────────────────────────────────────────────

_MAP = {"deputy_owner": "<:rank_deputy_owner:123>", "goblin": "<:rank_goblin:456>"}


@pytest.mark.parametrize("rank", ["deputy_owner", "Deputy Owner", "DEPUTY-OWNER"])
def test_lookup_is_spelling_insensitive(rank):
    assert rank_emojis.emoji_for_rank(rank, _MAP) == "<:rank_deputy_owner:123>"


def test_unknown_rank_returns_none_rather_than_a_broken_token():
    # "Not Ranked" has no wiki icon and WOM's default role is "member" — both
    # are normal, and both must render as a plain line.
    assert rank_emojis.emoji_for_rank("Not Ranked", _MAP) is None
    assert rank_emojis.emoji_for_rank("member", _MAP) is None
    assert rank_emojis.emoji_for_rank(None, _MAP) is None
    assert rank_emojis.emoji_for_rank("", _MAP) is None


def test_missing_map_file_is_not_an_error(tmp_path):
    assert rank_emojis.load_map(str(tmp_path / "nope.json")) == {}


def test_map_file_keys_are_normalized_on_load(tmp_path):
    path = tmp_path / "rank_emojis.json"
    path.write_text(json.dumps({"Deputy Owner": "<:rank_deputy_owner:1>", "empty": ""}))
    loaded = rank_emojis.load_map(str(path))
    assert loaded["deputy_owner"] == "<:rank_deputy_owner:1>"
    assert "empty" not in loaded  # falsy values are dropped, not stored


def test_seeded_map_covers_the_real_rank_set():
    """The committed map is what production reads; a truncated seed would
    silently drop icons for whole clans."""
    mapping = rank_emojis.load_map()
    if not mapping:
        pytest.skip("rank emojis not seeded in this environment")
    assert len(mapping) >= 250
    for key in ("owner", "deputy_owner", "recruit"):
        assert mapping[key].startswith(f"<:rank_{key}:")


# ── clan rank cache ─────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self, data=None):
        self.data = data or {}
        self.expired = None

    def pipeline(self):
        return self

    def delete(self, key):
        self.data.pop(key, None)

    def hset(self, key, mapping=None):
        self.data.setdefault(key, {}).update(mapping or {})

    def expire(self, key, ttl):
        self.expired = ttl

    def execute(self):
        return True

    def hget(self, key, field):
        return self.data.get(key, {}).get(field)


def test_store_group_ranks_normalizes_names_and_drops_default_role(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(clan_ranks, "_redis", lambda: fake)
    stored = clan_ranks.store_group_ranks(
        42, {"Beast Owned": "deputy_owner", "Nobody": "member", "Blank": ""}
    )
    assert stored == 1
    assert fake.data["clanrank:42"] == {"beast owned": "deputy_owner"}
    assert fake.expired == clan_ranks.RANK_TTL_SECONDS


def test_rank_lookup_survives_the_plugin_underscore_spelling(monkeypatch):
    """The plugin relays the client spelling (Beast_Owned); WOM and the DB
    carry the display spelling (Beast Owned)."""
    fake = _FakeRedis({"clanrank:42": {"beast owned": "deputy_owner"}})
    monkeypatch.setattr(clan_ranks, "_redis", lambda: fake)
    assert clan_ranks.rank_for_player(42, "Beast_Owned") == "deputy_owner"
    assert clan_ranks.rank_for_player(42, "beast owned") == "deputy_owner"
    assert clan_ranks.rank_for_player(42, "Someone Else") is None


def test_rank_lookup_never_raises_when_redis_is_down(monkeypatch):
    def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(clan_ranks, "_redis", boom)
    assert clan_ranks.rank_for_player(42, "Beast Owned") is None
    assert clan_ranks.store_group_ranks(42, {"Beast Owned": "owner"}) == 0
