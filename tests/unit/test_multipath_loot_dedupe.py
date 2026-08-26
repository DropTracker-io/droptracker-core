"""Unit tests for the multi-part boss same-kill defense
(data/submissions/raid_dedupe.flag_multipath_loot_duplicates).

RuneLite delivers one kill of the Grotesque Guardians through two loot events —
``NpcLootReceived`` naming "Dusk", ``LootReceived`` naming the encounter — so a
pre-5.4.0 plugin submits the same kill twice with two GUIDs, under two names.
These tests exercise the content fingerprint that collapses them: the second
copy is flagged even though it names the boss differently, while genuine
double-kills of ordinary NPCs, different loot, other accounts and non-drop
embeds all pass. Redis trouble fails open.
"""

import sys

import pytest

from data.submissions.raid_dedupe import (
    MULTIPATH_FLAG,
    MULTIPATH_REJECT_MESSAGE,
    RELOOT_FLAG,
    RELOOT_REJECT_MESSAGE,
    _multipath_source_key,
    duplicate_reject_message,
    flag_multipath_loot_duplicates,
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


def _gg_bundle(acc_hash="668351129353112525", source="Grotesque Guardians",
               world_type="main", items=((21726, 58), (21739, 1), (560, 101)),
               killcount=523):
    """The real duplicated kill from the 2026-08-03 report: Granite dust x58,
    Granite ring, Death rune x101. The Dusk copy carried no kill count."""
    return [
        {
            "type": "drop",
            "player_name": "Testy",
            "acc_hash": acc_hash,
            "source": source,
            "id": item_id,
            "quantity": qty,
            "killcount": killcount,
            "world_type": world_type,
        }
        for item_id, qty in items
    ]


class TestFlagMultipathLootDuplicates:
    def test_first_bundle_is_not_flagged(self, fake_redis):
        items = _gg_bundle()
        assert flag_multipath_loot_duplicates(items) == 0
        assert not any(MULTIPATH_FLAG in item for item in items)

    def test_second_copy_of_the_same_kill_is_flagged(self, fake_redis):
        flag_multipath_loot_duplicates(_gg_bundle())
        repeat = _gg_bundle()
        assert flag_multipath_loot_duplicates(repeat) == len(repeat)
        assert all(item.get(MULTIPATH_FLAG) for item in repeat)

    def test_sub_npc_name_collapses_onto_the_encounter(self, fake_redis):
        """The whole point: the two loot events name the kill differently.
        NpcLootReceived says "Dusk", LootReceived says "Grotesque Guardians"."""
        assert flag_multipath_loot_duplicates(_gg_bundle(source="Dusk")) == 0
        repeat = _gg_bundle(source="Grotesque Guardians")
        assert flag_multipath_loot_duplicates(repeat) == len(repeat)

    def test_missing_killcount_on_one_copy_still_matches(self, fake_redis):
        """The Dusk path sends no KC; the encounter path sends one. The
        fingerprint is items-only, so the pair must still collapse."""
        flag_multipath_loot_duplicates(_gg_bundle(source="Dusk", killcount=None))
        repeat = _gg_bundle(killcount=523)
        assert flag_multipath_loot_duplicates(repeat) == len(repeat)

    def test_loot_never_seen_before_passes(self, fake_redis):
        """Matching is per item, so an item the window hasn't seen lands even
        when it arrives alongside items it has."""
        flag_multipath_loot_duplicates(_gg_bundle())
        fresh = _gg_bundle(items=((4151, 1),))
        assert flag_multipath_loot_duplicates(fresh) == 0

    def test_a_different_quantity_is_a_different_item_fingerprint(self, fake_redis):
        """Quantity is part of the fingerprint: the same item id at a new
        quantity is not the copy we already took."""
        flag_multipath_loot_duplicates(_gg_bundle(items=((21726, 58),)))
        assert flag_multipath_loot_duplicates(_gg_bundle(items=((21726, 59),))) == 0

    def test_partial_overlap_flags_only_the_repeated_items(self, fake_redis):
        """The bug this exists for (Shiny Quag, 2026-08-26).

        RuneLite's two loot events do NOT agree on the item list: the
        encounter path carries Granite dust, the NPC path omits it. Bundle
        hashing therefore never matched and the shared items were written
        twice. Per item, the repeats are caught and the item unique to one
        path still lands exactly once.
        """
        first = _gg_bundle(source="Dusk", items=((2364, 5), (21742, 1)))
        assert flag_multipath_loot_duplicates(first) == 0

        second = _gg_bundle(
            source="Grotesque Guardians",
            items=((21726, 99), (2364, 5), (21742, 1)),
        )
        assert flag_multipath_loot_duplicates(second) == 2
        by_item = {item["id"]: item.get(MULTIPATH_FLAG, False) for item in second}
        assert by_item[21726] is False, "Granite dust only ever arrived once"
        assert by_item[2364] is True
        assert by_item[21742] is True

    def test_repeat_inside_one_payload_is_flagged(self, fake_redis):
        """Both loot events can land in a single payload, in which case the
        duplicate has to be caught within the batch rather than across two."""
        both = _gg_bundle(items=((21742, 1),)) + _gg_bundle(items=((21742, 1),))
        assert flag_multipath_loot_duplicates(both) == 1
        assert both[0].get(MULTIPATH_FLAG) is None
        assert both[1].get(MULTIPATH_FLAG) is True

    def test_npc_name_is_accepted_as_a_source_alias(self, fake_redis):
        """drop_processor reads `source` OR `npc_name`; a payload naming the
        boss the second way must not slip past the dedup."""
        first = [dict(item, npc_name=item.pop("source")) for item in _gg_bundle()]
        assert flag_multipath_loot_duplicates(first) == 0
        repeat = [dict(item, npc_name=item.pop("source")) for item in _gg_bundle()]
        assert flag_multipath_loot_duplicates(repeat) == len(repeat)

    def test_other_account_is_untouched(self, fake_redis):
        flag_multipath_loot_duplicates(_gg_bundle())
        assert flag_multipath_loot_duplicates(_gg_bundle(acc_hash="999")) == 0

    def test_seasonal_world_is_a_separate_namespace(self, fake_redis):
        flag_multipath_loot_duplicates(_gg_bundle(world_type="main"))
        assert flag_multipath_loot_duplicates(_gg_bundle(world_type="seasonal")) == 0

    def test_ordinary_npcs_are_never_deduped(self, fake_redis):
        """The safety property: AoE slayer legitimately produces two identical
        bundles in one tick, and both must be recorded."""
        for source in ("Abyssal demon", "Maniacal monkey", "Vorkath", "Zulrah"):
            flag_multipath_loot_duplicates(_gg_bundle(source=source))
            assert flag_multipath_loot_duplicates(_gg_bundle(source=source)) == 0

    def test_other_multipath_encounters_are_covered(self, fake_redis):
        for source, canonical in (("Branda the Fire Queen", "Royal Titans"),
                                  ("Crystalline Hunllef", "The Gauntlet"),
                                  ("Corrupted Hunllef", "The Corrupted Gauntlet"),
                                  ("Araxxor", "Araxxor"),
                                  ("The Whisperer", "The Whisperer")):
            items = ((995, 1000),)
            assert flag_multipath_loot_duplicates(
                _gg_bundle(source=source, items=items)) == 0
            repeat = _gg_bundle(source=canonical, items=items)
            assert flag_multipath_loot_duplicates(repeat) == len(repeat)

    def test_non_drop_embeds_are_ignored(self, fake_redis):
        pb = [dict(item, type="pb") for item in _gg_bundle()]
        assert flag_multipath_loot_duplicates(pb) == 0
        assert flag_multipath_loot_duplicates(pb) == 0

    def test_embeds_without_acc_hash_are_ignored(self, fake_redis):
        items = [dict(item, acc_hash=None) for item in _gg_bundle()]
        assert flag_multipath_loot_duplicates(items) == 0
        # ...and left no key behind that would swallow the real bundle.
        assert flag_multipath_loot_duplicates(_gg_bundle()) == 0

    def test_redis_failure_fails_open(self, monkeypatch):
        _patch_redis(monkeypatch, _BrokenRedis())
        assert flag_multipath_loot_duplicates(_gg_bundle()) == 0
        assert flag_multipath_loot_duplicates(_gg_bundle()) == 0

    def test_empty_payload(self, fake_redis):
        assert flag_multipath_loot_duplicates([]) == 0
        assert flag_multipath_loot_duplicates(None) == 0

    def test_the_two_passes_do_not_share_a_namespace(self, fake_redis):
        """A raid bundle and a boss bundle must never collide in Redis."""
        from data.submissions.raid_dedupe import flag_raid_reloot_duplicates

        flag_multipath_loot_duplicates(_gg_bundle())
        tob = _gg_bundle(source="Theatre of Blood")
        assert flag_raid_reloot_duplicates(tob) == 0


class TestMultipathSourceKey:
    def test_sub_npcs_fold_to_their_encounter(self):
        assert (_multipath_source_key("Dusk")
                == _multipath_source_key("Grotesque Guardians"))
        assert (_multipath_source_key("Branda the Fire Queen")
                == _multipath_source_key("Eldric the Ice King")
                == _multipath_source_key("Royal Titans"))
        assert (_multipath_source_key("Crystalline Hunllef")
                == _multipath_source_key("The Gauntlet"))

    def test_spelling_variants_fold(self):
        assert _multipath_source_key("grotesque guardians") == \
            _multipath_source_key("Grotesque Guardians")

    def test_non_multipath_sources_are_none(self):
        assert _multipath_source_key("Zulrah") is None
        assert _multipath_source_key("Abyssal demon") is None
        assert _multipath_source_key("Theatre of Blood") is None
        assert _multipath_source_key("") is None
        assert _multipath_source_key(None) is None


class TestDuplicateRejectMessage:
    def test_clean_embed(self):
        assert duplicate_reject_message({"type": "drop"}) is None

    def test_each_flag_gets_its_own_message(self):
        assert duplicate_reject_message({RELOOT_FLAG: True}) == RELOOT_REJECT_MESSAGE
        assert duplicate_reject_message({MULTIPATH_FLAG: True}) == MULTIPATH_REJECT_MESSAGE
