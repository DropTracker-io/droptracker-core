"""Unit tests for the re-looted raid chest defense (data/submissions/raid_dedupe.py).

A raid reward chest opened in the loot room and again at the bank collection
chest produces two identical drop bundles with fresh GUIDs (and, on older
plugin builds, an incremented killcount) minutes apart. These tests exercise
the content fingerprint that catches the repeat: identical bundles are
flagged, anything else — different rolls, different raids, different accounts,
non-raid sources — passes, and Redis trouble fails open.
"""

import sys

import pytest

from data.submissions.raid_dedupe import (
    RELOOT_FLAG,
    RELOOT_TTL_SECONDS,
    _raid_base_key,
    flag_raid_reloot_duplicates,
)


class _FakeRedis:
    """Minimal redis client with real SET NX semantics."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True


class _BrokenRedis:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis down")


def _patch_redis(monkeypatch, client):
    # The conftest registers "utils.redis" in sys.modules without attaching it
    # to the "utils" package, so dotted-path setattr can't reach it — patch
    # the stub module object directly.
    monkeypatch.setattr(
        sys.modules["utils.redis"],
        "redis_client",
        type("RC", (), {"client": client})(),
        raising=False,
    )


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    return fake


def _tob_bundle(acc_hash="4062539364958246995", source="Theatre of Blood",
                world_type="main", items=((22446, 1), (565, 500), (560, 300)),
                killcount=1004):
    """Processed embeds of one webhook payload, as process_webhook_data
    flattens them (one dict per item embed)."""
    return [
        {
            "type": "drop",
            "player_name": "Fazebook",
            "acc_hash": acc_hash,
            "source": source,
            "id": item_id,
            "quantity": qty,
            "killcount": killcount,
            "world_type": world_type,
        }
        for item_id, qty in items
    ]


class TestFlagRaidRelootDuplicates:
    def test_first_bundle_is_not_flagged(self, fake_redis):
        items = _tob_bundle()
        assert flag_raid_reloot_duplicates(items) == 0
        assert not any(RELOOT_FLAG in item for item in items)

    def test_identical_bundle_is_flagged_on_every_embed(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle())
        repeat = _tob_bundle()
        assert flag_raid_reloot_duplicates(repeat) == len(repeat)
        assert all(item.get(RELOOT_FLAG) for item in repeat)

    def test_phantom_killcount_increment_does_not_defeat_dedup(self, fake_redis):
        # Older plugin builds bump the cached KC on the second chest open, so
        # the duplicate arrives claiming kc+1. The fingerprint must ignore it.
        flag_raid_reloot_duplicates(_tob_bundle(killcount=1004))
        repeat = _tob_bundle(killcount=1005)
        assert flag_raid_reloot_duplicates(repeat) == len(repeat)

    def test_embed_order_does_not_matter(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle(items=((565, 500), (22446, 1))))
        repeat = _tob_bundle(items=((22446, 1), (565, 500)))
        assert flag_raid_reloot_duplicates(repeat) == len(repeat)

    def test_mode_variant_folds_to_base_raid(self, fake_redis):
        # First event names the mode (chat-derived), the bank chest event the
        # base raid — same chest, one dedup scope.
        flag_raid_reloot_duplicates(_tob_bundle(source="Theatre of Blood: Hard Mode"))
        repeat = _tob_bundle(source="Theatre of Blood")
        assert flag_raid_reloot_duplicates(repeat) == len(repeat)

    def test_different_rolls_are_not_flagged(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle())
        fresh = _tob_bundle(items=((22446, 1), (565, 499), (560, 300)))
        assert flag_raid_reloot_duplicates(fresh) == 0

    def test_different_raids_do_not_share_scope(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle())
        other_raid = _tob_bundle(source="Tombs of Amascut")
        assert flag_raid_reloot_duplicates(other_raid) == 0

    def test_different_accounts_do_not_share_scope(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle())
        other_acc = _tob_bundle(acc_hash="1111111111")
        assert flag_raid_reloot_duplicates(other_acc) == 0

    def test_world_types_do_not_share_scope(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle(world_type="main"))
        seasonal = _tob_bundle(world_type="seasonal")
        assert flag_raid_reloot_duplicates(seasonal) == 0

    def test_non_raid_sources_are_never_fingerprinted(self, fake_redis):
        zulrah = _tob_bundle(source="Zulrah")
        assert flag_raid_reloot_duplicates(zulrah) == 0
        assert flag_raid_reloot_duplicates(_tob_bundle(source="Zulrah")) == 0
        assert not fake_redis.store

    def test_non_drop_embeds_are_ignored(self, fake_redis):
        pb = [{"type": "npc_kill", "acc_hash": "1", "source": "Theatre of Blood"}]
        assert flag_raid_reloot_duplicates(pb) == 0
        assert not fake_redis.store

    def test_missing_acc_hash_is_skipped(self, fake_redis):
        items = _tob_bundle()
        for item in items:
            item.pop("acc_hash")
        assert flag_raid_reloot_duplicates(items) == 0
        assert flag_raid_reloot_duplicates(_tob_bundle()) == 0  # still first sight

    def test_keys_carry_the_backstop_ttl(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle())
        assert list(fake_redis.ttls.values()) == [RELOOT_TTL_SECONDS]

    def test_redis_errors_fail_open(self, monkeypatch):
        _patch_redis(monkeypatch, _BrokenRedis())
        assert flag_raid_reloot_duplicates(_tob_bundle()) == 0
        assert flag_raid_reloot_duplicates(_tob_bundle()) == 0

    def test_missing_redis_client_fails_open(self, monkeypatch):
        _patch_redis(monkeypatch, None)
        assert flag_raid_reloot_duplicates(_tob_bundle()) == 0
        assert flag_raid_reloot_duplicates(_tob_bundle()) == 0


class TestRaidBaseKey:
    def test_base_raids(self):
        assert _raid_base_key("Theatre of Blood") == "theatre-of-blood"
        assert _raid_base_key("Tombs of Amascut") == "tombs-of-amascut"
        assert _raid_base_key("Chambers of Xeric") == "chambers-of-xeric"

    def test_mode_variants_fold_to_base(self):
        assert _raid_base_key("Theatre of Blood: Entry Mode") == "theatre-of-blood"
        assert _raid_base_key("Theatre of Blood: Hard Mode") == "theatre-of-blood"
        assert _raid_base_key("Tombs of Amascut: Expert Mode") == "tombs-of-amascut"
        assert _raid_base_key("Chambers of Xeric Challenge Mode") == "chambers-of-xeric"

    def test_non_raids_are_none(self):
        assert _raid_base_key("Zulrah") is None
        assert _raid_base_key("Barrows") is None
        assert _raid_base_key("") is None
        assert _raid_base_key(None) is None
