"""Per-group always-announce list: listed items/NPCs post despite the value gate.

The inverse of the notification blacklist. Group leaders list items (the
"notable" zero-value kits and pieces the plugin force-screenshots) and NPCs on
the group settings page; a drop of a listed item — or from a listed NPC — is
announced even when it falls below the group's minimum_value_to_notify. Three
things have to hold, and all are asserted here:

1. the *matching rule* (``db.notification_always_list``) is the blacklist's —
   imported, not reimplemented — so the two lists can never disagree about
   what a name means;
2. matching **fails closed**: this gate only ever adds announcements, so on
   any doubt (empty list, DB fault, no group) the drop behaves as it does
   today;
3. the per-group TTL cache actually caches — the rule runs for every drop that
   failed the value gate, which is most drops, and must not hit the database
   each time.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from db.notification_always_list import (
    ENTRY_TYPES,
    always_announce_reason,
    entry_key,
    invalidate_cache,
    load_group_always_list,
)


def _db(entries=(), raises=False):
    """Session whose always-list query resolves to `entries` [(type, key), …]."""
    db = MagicMock()

    def _query(*a, **k):
        if raises:
            raise RuntimeError("database went away")
        q = MagicMock()
        q.filter.return_value = SimpleNamespace(all=lambda: list(entries))
        return q

    db.query.side_effect = _query
    return db


@pytest.fixture(autouse=True)
def _fresh_cache():
    # The module caches per group for 30s; tests must not see each other's rows.
    invalidate_cache()
    yield
    invalidate_cache()


class TestKeys:
    def test_no_region_entries(self):
        # Only the drop value gate is bypassed, and drops carry no region.
        assert ENTRY_TYPES == ("item", "npc")

    @pytest.mark.parametrize(
        "name", ["Twisted bow", "Twisted Bow", "twisted_bow", "  TWISTED  BOW "]
    )
    def test_item_normalization_is_the_blacklists(self, name):
        from db.notification_blacklist import item_key

        assert entry_key("item", name) == item_key(name) == "twisted-bow"

    def test_npc_normalization_is_the_blacklists(self):
        from db.notification_blacklist import npc_key

        assert entry_key("npc", "The Whisperer") == npc_key("Whisperer") == "whisperer"

    @pytest.mark.parametrize("name", ["Unknown", "", None, "   ", "n/a"])
    def test_placeholder_npcs_are_not_matchable(self, name):
        assert entry_key("npc", name) == ""


class TestReason:
    def test_listed_item_fires_whatever_the_spelling(self):
        db = _db([("item", "twisted-ancestral-colour-kit")])
        reason = always_announce_reason(
            db, 42, item_name="Twisted_ancestral colour kit", npc_name="Chambers of Xeric"
        )
        assert reason == "always-announce item 'twisted-ancestral-colour-kit'"

    def test_listed_npc_fires_for_any_of_its_drops(self):
        db = _db([("npc", "zulrah")])
        assert always_announce_reason(db, 42, item_name="Snakeskin", npc_name="Zulrah")

    def test_listed_base_raid_covers_its_mode_variant(self):
        # Listing "Chambers of Xeric" covers Challenge Mode, same one-way fold
        # as the blacklist's.
        db = _db([("npc", "chambers-of-xeric")])
        assert always_announce_reason(
            db, 42, item_name="Pure essence", npc_name="Chambers of Xeric Challenge Mode"
        )

    def test_mode_variant_entry_does_not_cover_the_base_raid(self):
        db = _db([("npc", "chambers-of-xeric-challenge-mode")])
        assert (
            always_announce_reason(
                db, 42, item_name="Pure essence", npc_name="Chambers of Xeric"
            )
            is None
        )

    def test_unlisted_drop_is_untouched(self):
        db = _db([("item", "twisted-bow"), ("npc", "zulrah")])
        assert always_announce_reason(db, 42, item_name="Bones", npc_name="Barrows") is None

    def test_no_group_fails_closed(self):
        assert always_announce_reason(_db(), None, item_name="Bones") is None
        assert always_announce_reason(_db(), 0, item_name="Bones") is None

    def test_empty_list_fails_closed(self):
        assert always_announce_reason(_db(), 42, item_name="Bones", npc_name="Barrows") is None

    def test_db_fault_fails_closed(self):
        # This gate only ever ADDS announcements: a database wobble must leave
        # every drop behaving exactly as it does today.
        db = _db(raises=True)
        assert always_announce_reason(db, 42, item_name="Twisted bow") is None

    def test_missing_names_do_not_match(self):
        db = _db([("item", "twisted-bow"), ("npc", "zulrah")])
        assert always_announce_reason(db, 42) is None


class TestCache:
    def test_second_lookup_within_ttl_skips_the_db(self):
        db = _db([("item", "twisted-bow")])
        load_group_always_list(db, 42)
        load_group_always_list(db, 42)
        assert db.query.call_count == 1

    def test_groups_are_cached_independently(self):
        db = _db([("item", "twisted-bow")])
        load_group_always_list(db, 42)
        load_group_always_list(db, 43)
        assert db.query.call_count == 2

    def test_invalidate_forces_a_reload(self):
        db = _db([("item", "twisted-bow")])
        load_group_always_list(db, 42)
        invalidate_cache(42)
        load_group_always_list(db, 42)
        assert db.query.call_count == 2

    def test_a_faulting_load_is_not_cached(self):
        # Fail-closed must be transient: the next drop retries the query.
        db = _db(raises=True)
        assert always_announce_reason(db, 42, item_name="Twisted bow") is None
        ok = _db([("item", "twisted-bow")])
        assert always_announce_reason(ok, 42, item_name="Twisted bow")
