"""Unit tests for the re-looted raid chest defense (data/submissions/raid_dedupe.py).

A raid reward chest opened in the loot room and again at the bank collection
chest produces two identical drop bundles with fresh GUIDs (and, on older
plugin builds, an incremented killcount) minutes apart. These tests exercise
the content fingerprint that catches the repeat: identical bundles are
flagged, anything else — different rolls, different raids, different accounts,
non-raid sources — passes, and Redis trouble fails open.

TestCompletionAwareRearm covers the other half: an identical bundle from a
LATER completion (two purples of the same unique, ticket #441) must pass.
"""

import logging
import sys

import pytest

from data.submissions.raid_dedupe import (
    RELOOT_FLAG,
    RELOOT_KC_TTL_SECONDS,
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

    def get(self, key):
        # The production client doesn't decode responses: values come back
        # as bytes.
        value = self.store.get(key)
        return None if value is None else str(value).encode()


class _BrokenRedis:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    def get(self, *args, **kwargs):
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
                killcount=1004, plugin_version=None):
    """Processed embeds of one webhook payload, as process_webhook_data
    flattens them (one dict per item embed). No ``p_v`` unless a
    plugin_version is given, which keeps the kill count out of play."""
    embeds = [
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
    if plugin_version is not None:
        for embed in embeds:
            embed["p_v"] = plugin_version
    return embeds


MODERN_BUILD = "6.0.6"
HILT = ((22477, 1),)
HARD_MODE = "Theatre of Blood: Hard Mode"


def _purple_chest(killcount, source="Theatre of Blood", items=HILT,
                  plugin_version=MODERN_BUILD, **kwargs):
    """A purple chest as a current plugin sends it: the unique alone, and
    every field a string (BaseEventHandler.addFields stringifies them)."""
    return _tob_bundle(source=source, items=items, killcount=str(killcount),
                       plugin_version=plugin_version, **kwargs)


def _loot_chest(killcount, **kwargs):
    """A regular (multi-item) chest from a current plugin."""
    return _tob_bundle(killcount=str(killcount), plugin_version=MODERN_BUILD, **kwargs)


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
        # Builds before 5.4.0 bump the cached KC on the second chest open, so
        # the duplicate arrives claiming kc+1. Their kill count must be ignored.
        flag_raid_reloot_duplicates(_tob_bundle(killcount=1004, plugin_version="5.3.0"))
        repeat = _tob_bundle(killcount=1005, plugin_version="5.3.0")
        assert flag_raid_reloot_duplicates(repeat) == len(repeat)

    def test_kill_count_is_ignored_without_a_plugin_version(self, fake_redis):
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


class TestCompletionAwareRearm:
    """An identical bundle is a re-open only if it is the SAME completion.

    A purple chest holds the unique alone, so two raids that roll the same
    unique send byte-identical bundles. Ticket #441: Yo Default's Avernic
    defender hilt at ToB kc 818 was followed by another at kc 819 25 minutes
    later, and the second was rejected as a re-opened chest.
    """

    def test_back_to_back_purple_is_accepted(self, fake_redis):
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 0
        second = _purple_chest(819)
        assert flag_raid_reloot_duplicates(second) == 0
        assert not any(RELOOT_FLAG in item for item in second)

    def test_back_to_back_challenge_mode_purple_is_accepted(self, fake_redis):
        # f Davy, 2026-08-17: an Ancestral hat at CM kc 736, then another at 737.
        def hat(kc):
            return _purple_chest(kc, source="Chambers of Xeric Challenge Mode",
                               items=((21018, 1),))
        assert flag_raid_reloot_duplicates(hat(736)) == 0
        assert flag_raid_reloot_duplicates(hat(737)) == 0

    def test_same_purple_with_raids_in_between_is_accepted(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818))
        flag_raid_reloot_duplicates(_loot_chest(819))
        assert flag_raid_reloot_duplicates(_purple_chest(820)) == 0

    def test_integer_kill_counts_work_too(self, fake_redis):
        flag_raid_reloot_duplicates(_tob_bundle(items=HILT, killcount=818,
                                                plugin_version=MODERN_BUILD))
        second = _tob_bundle(items=HILT, killcount=819, plugin_version=MODERN_BUILD)
        assert flag_raid_reloot_duplicates(second) == 0

    def test_snapshot_builds_are_trusted(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818, plugin_version="6.0.7-SNAPSHOT"))
        assert flag_raid_reloot_duplicates(
            _purple_chest(819, plugin_version="6.0.7-SNAPSHOT")) == 0

    def test_reopen_after_a_restart_is_flagged(self, fake_redis):
        # A restarted client re-reads RuneLite's stored count, so the re-open
        # reports the chest's own completion count.
        flag_raid_reloot_duplicates(_purple_chest(818))
        reopen = _purple_chest(818)
        assert flag_raid_reloot_duplicates(reopen) == len(reopen)
        assert all(item.get(RELOOT_FLAG) for item in reopen)

    def test_reopen_of_a_regular_chest_is_flagged(self, fake_redis):
        flag_raid_reloot_duplicates(_loot_chest(818))
        reopen = _loot_chest(818)
        assert flag_raid_reloot_duplicates(reopen) == len(reopen)

    def test_reopen_of_the_back_to_back_chest_is_flagged(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818))
        flag_raid_reloot_duplicates(_purple_chest(819))
        assert flag_raid_reloot_duplicates(_purple_chest(819)) == 1

    def test_back_to_back_chest_rearms_the_fingerprint(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818))
        fake_redis.ttls.clear()
        flag_raid_reloot_duplicates(_purple_chest(819))
        fingerprints = [k for k in fake_redis.ttls if k.startswith("raidloot:reloot:")]
        assert len(fingerprints) == 1
        assert fake_redis.ttls[fingerprints[0]] == RELOOT_TTL_SECONDS

    def test_stale_kill_count_does_not_lower_the_watermark(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818))
        assert flag_raid_reloot_duplicates(_purple_chest(817)) == 1
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 1

    def test_restart_reopen_of_a_mode_raid_is_flagged(self, fake_redis):
        # After a restart the client has lost the chat-derived mode: the
        # Hard Mode chest is claimed under the base raid's name and count,
        # which a normal-mode chest already recorded.
        flag_raid_reloot_duplicates(_loot_chest(818))
        flag_raid_reloot_duplicates(_purple_chest(300, source=HARD_MODE))
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 1

    def test_mode_counts_never_stand_in_for_each_other(self, fake_redis):
        # No normal-mode history: the Hard Mode count (300) says nothing
        # about a base-raid count, so the repeat stays flagged.
        flag_raid_reloot_duplicates(_purple_chest(300, source=HARD_MODE))
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 1

    def test_switching_modes_between_the_same_purple_is_accepted(self, fake_redis):
        flag_raid_reloot_duplicates(_loot_chest(818))
        flag_raid_reloot_duplicates(_purple_chest(300, source=HARD_MODE))
        assert flag_raid_reloot_duplicates(_purple_chest(819)) == 0

    @pytest.mark.parametrize("unknown", ["0", "N/A", None, ""])
    def test_unknown_kill_count_is_flagged(self, fake_redis, unknown):
        # The plugin sends 0 for "no count"; addFields turns null into "N/A".
        flag_raid_reloot_duplicates(_purple_chest(818))
        repeat = _purple_chest(819)
        for item in repeat:
            item["killcount"] = unknown
        assert flag_raid_reloot_duplicates(repeat) == 1

    def test_missing_kill_count_is_flagged(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818))
        repeat = _purple_chest(819)
        for item in repeat:
            del item["killcount"]
        assert flag_raid_reloot_duplicates(repeat) == 1

    @pytest.mark.parametrize("version", ["5.3.0", "5.3", "unknown", "N/A"])
    def test_untrusted_builds_keep_the_old_verdict(self, fake_redis, version):
        flag_raid_reloot_duplicates(_purple_chest(818, plugin_version=version))
        assert flag_raid_reloot_duplicates(_purple_chest(819, plugin_version=version)) == 1

    def test_embeds_disagreeing_on_kill_count_are_flagged(self, fake_redis):
        flag_raid_reloot_duplicates(_loot_chest(818))
        repeat = _loot_chest(819)
        repeat[0]["killcount"] = "820"
        assert flag_raid_reloot_duplicates(repeat) == len(repeat)

    def test_watermarks_do_not_cross_accounts_or_worlds(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818))
        # Were the watermark shared, these lower counts would replace 818 and
        # wave this account's same-completion re-open through.
        flag_raid_reloot_duplicates(_loot_chest(12, acc_hash="1111111111"))
        flag_raid_reloot_duplicates(_loot_chest(12, world_type="seasonal"))
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 1

    def test_watermark_has_its_own_ttl(self, fake_redis):
        flag_raid_reloot_duplicates(_purple_chest(818))
        assert sorted(fake_redis.ttls.values()) == [RELOOT_TTL_SECONDS,
                                                    RELOOT_KC_TTL_SECONDS]
        watermark = next(k for k in fake_redis.store if k.startswith("raidloot:kc:"))
        assert watermark.endswith(":theatre-of-blood")
        assert fake_redis.store[watermark] == 818

    def test_rejection_logs_who_and_what(self, fake_redis, caplog):
        caplog.set_level(logging.INFO, logger="data.submissions.raid_dedupe")
        flag_raid_reloot_duplicates(_purple_chest(818))
        flag_raid_reloot_duplicates(_purple_chest(818))
        (record,) = [r for r in caplog.records if "rejected" in r.getMessage()]
        message = record.getMessage()
        assert "player='Fazebook'" in message
        assert "acc=4062539364958246995" in message
        assert "kc=818" in message and "items=22477:1" in message

    def test_accepted_repeat_is_logged(self, fake_redis, caplog):
        caplog.set_level(logging.INFO, logger="data.submissions.raid_dedupe")
        flag_raid_reloot_duplicates(_purple_chest(818))
        flag_raid_reloot_duplicates(_purple_chest(819))
        messages = [r.getMessage() for r in caplog.records]
        assert any("later completion" in m and "kc=819" in m for m in messages)
        assert not any("rejected" in m for m in messages)

    def test_redis_errors_fail_open_for_current_builds(self, monkeypatch):
        _patch_redis(monkeypatch, _BrokenRedis())
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 0
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 0

    def test_watermark_read_failure_fails_open(self, monkeypatch):
        class _NoReads(_FakeRedis):
            def get(self, key):
                raise ConnectionError("redis down")

        _patch_redis(monkeypatch, _NoReads())
        flag_raid_reloot_duplicates(_purple_chest(818))
        assert flag_raid_reloot_duplicates(_purple_chest(818)) == 0


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
