"""Per-group notification blacklist: muted items/NPCs never reach Discord.

Group leaders curate a list of items and NPCs on the group settings page. The
submission still lands — recorded, scored, on the lootboard and the
leaderboards — but that group's Discord channel never hears about it. Two
things have to hold for that to be true, and both are asserted here:

1. the *matching rule* (``db.notification_blacklist``) recognises the same
   thing whichever spelling a submission arrives with, and never matches
   something a leader did not blacklist;
2. the *chokepoint* (``create_notification``) consults it for every submission
   type, and only for group-channel notifications — a group's list must not be
   able to mute a member's own DMs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from db.notification_blacklist import (
    BLACKLISTABLE_TYPES,
    blacklist_reason,
    entry_key,
    item_key,
    npc_key,
    payload_subjects,
)


def _db(entries=(), raises=False):
    """Session whose blacklist query resolves to `entries` [(type, key), …]."""
    db = MagicMock()

    def _query(*a, **k):
        if raises:
            raise RuntimeError("database went away")
        q = MagicMock()
        q.filter.return_value = SimpleNamespace(all=lambda: list(entries))
        return q

    db.query.side_effect = _query
    return db


# ── The matching rule ───────────────────────────────────────────────────────

class TestKeys:
    @pytest.mark.parametrize(
        "name", ["Twisted bow", "Twisted Bow", "twisted_bow", "  TWISTED  BOW "]
    )
    def test_item_spellings_collapse_to_one_key(self, name):
        # The plugin sends underscored names, the catalog stores spaced ones and
        # a leader types whatever they type ([[droptracker-rsn-spelling-divergence]]).
        assert item_key(name) == "twisted-bow"

    def test_distinct_items_keep_distinct_keys(self):
        assert item_key("Dragon pickaxe") != item_key("Dragon pickaxe (or)")

    @pytest.mark.parametrize(
        "name",
        [
            "Chambers of Xeric (Challenge mode)",
            "Chambers of Xeric Challenge Mode",
            "chambers-of-xeric-challenge-mode",
        ],
    )
    def test_npc_spellings_collapse(self, name):
        assert npc_key(name) == "chambers-of-xeric-challenge-mode"

    def test_npc_article_and_alias_are_folded(self):
        # npc_match_key is the codebase's one NPC identity rule; the blacklist
        # must inherit it rather than invent a second one.
        assert npc_key("The Whisperer") == npc_key("Whisperer")
        assert npc_key("Crystalline Hunllef") == npc_key("The Gauntlet")

    @pytest.mark.parametrize("name", ["Unknown", "unknown", "", None, "   ", "n/a"])
    def test_placeholder_sources_are_not_matchable(self, name):
        # A clog with no known source arrives as "Unknown". If that were a real
        # key, one entry would mute every unsourced submission in the group.
        assert npc_key(name) == ""

    def test_entry_key_dispatches_on_type(self):
        assert entry_key("npc", "The Whisperer") == "whisperer"
        # Items get no article stripping — "The stuff" is a real item name.
        assert entry_key("item", "The stuff") == "the-stuff"


class TestPayloadSubjects:
    def test_drop_payload_yields_item_and_npc(self):
        items, npcs = payload_subjects(
            {"item_name": "Twisted bow", "npc_name": "Chambers of Xeric"}
        )
        assert items == {"twisted-bow"}
        assert npcs == {"chambers-of-xeric"}

    def test_pet_payload_treats_the_pet_as_an_item(self):
        # A pet IS an item; a leader blacklisting "Baby mole" means the pet post.
        items, npcs = payload_subjects(
            {"pet_name": "Baby mole", "source": "Giant Mole", "npc_name": "Giant Mole"}
        )
        assert items == {"baby-mole"}
        assert npcs == {"giant-mole"}

    def test_pb_payload_uses_boss_name(self):
        _, npcs = payload_subjects({"boss_name": "Zulrah"})
        assert npcs == {"zulrah"}

    def test_death_payload_uses_source(self):
        _, npcs = payload_subjects({"source": "Vet'ion"})
        # Apostrophes slug to '-', the same rule the rest of the codebase uses.
        assert npcs == {"vet-ion"}

    def test_mode_variant_also_tests_its_base_raid(self):
        # Blacklisting "Chambers of Xeric" must silence Challenge Mode too, so
        # the incoming variant is tested against its base as well as itself.
        _, npcs = payload_subjects({"npc_name": "Chambers of Xeric Challenge Mode"})
        assert npcs == {"chambers-of-xeric-challenge-mode", "chambers-of-xeric"}

    @pytest.mark.parametrize("data", [None, {}, "not a dict", {"tier": "Grandmaster"}])
    def test_payloads_with_no_subject_yield_nothing(self, data):
        assert payload_subjects(data) == (set(), set())


class TestBlacklistReason:
    DROP = {"item_name": "Bones", "npc_name": "Barrows"}

    def test_blacklisted_item_is_reported(self):
        db = _db([("item", "bones")])
        assert blacklist_reason(db, 303, "drop", self.DROP) == "blacklisted item 'bones'"

    def test_blacklisted_npc_is_reported(self):
        db = _db([("npc", "barrows")])
        assert blacklist_reason(db, 303, "drop", self.DROP) == "blacklisted NPC 'barrows'"

    def test_unrelated_entries_do_not_match(self):
        db = _db([("item", "twisted-bow"), ("npc", "zulrah")])
        assert blacklist_reason(db, 303, "drop", self.DROP) is None

    def test_item_and_npc_lists_do_not_cross_over(self):
        # "Barrows" filed as an ITEM must not mute the Barrows NPC, and vice
        # versa — otherwise blacklisting an item silences a whole boss.
        db = _db([("item", "barrows")])
        assert blacklist_reason(db, 303, "drop", self.DROP) is None

    def test_base_raid_entry_covers_its_mode_variant(self):
        db = _db([("npc", "chambers-of-xeric")])
        data = {"item_name": "Kodai insignia", "npc_name": "Chambers of Xeric (Challenge mode)"}
        assert blacklist_reason(db, 303, "drop", data) is not None

    def test_mode_variant_entry_does_not_cover_the_base_raid(self):
        # One-way on purpose: muting Challenge Mode is a narrower request than
        # muting the raid, and widening it would delete posts nobody muted.
        db = _db([("npc", "chambers-of-xeric-challenge-mode")])
        data = {"item_name": "Kodai insignia", "npc_name": "Chambers of Xeric"}
        assert blacklist_reason(db, 303, "drop", data) is None

    def test_no_group_id_is_never_filtered(self):
        db = _db([("item", "bones")])
        for group_id in (0, None):
            assert blacklist_reason(db, group_id, "drop", self.DROP) is None

    @pytest.mark.parametrize("notification_type", sorted(BLACKLISTABLE_TYPES))
    def test_every_submission_announcement_is_in_scope(self, notification_type):
        db = _db([("item", "bones")])
        assert blacklist_reason(db, 303, notification_type, self.DROP) is not None

    @pytest.mark.parametrize(
        "notification_type",
        ["event_task", "event_line", "dm_drop", "new_item", "points_earned"],
    )
    def test_out_of_scope_types_are_never_filtered(self, notification_type):
        # An event tile completion is a curated result the group asked for;
        # muting it because it names a blacklisted item deletes information
        # rather than reducing noise.
        db = _db([("item", "bones")])
        assert blacklist_reason(db, 303, notification_type, self.DROP) is None

    def test_db_error_fails_open(self):
        # A transient fault must not silently mute every group's Discord.
        db = _db([("item", "bones")], raises=True)
        assert blacklist_reason(db, 303, "drop", self.DROP) is None

    def test_empty_blacklist_costs_nothing(self):
        assert blacklist_reason(_db(), 303, "drop", self.DROP) is None


# ── The chokepoint ──────────────────────────────────────────────────────────

class TestCreateNotificationHonoursBlacklist:
    """The gate as the submission processors actually hit it."""

    @pytest.fixture
    def common(self):
        import data.submissions.common as common

        return common

    async def _create(self, common, reason, *, group_id=303,
                      notification_type="drop", data=None):
        db = MagicMock()
        with patch.object(common, "group_has_notification_channel", return_value=True), \
             patch.object(common, "player_hidden_for_group", return_value=False), \
             patch.object(common, "notification_blacklisted", return_value=reason), \
             patch.object(common, "NotificationQueue", MagicMock()):
            common.stored_notifications = {}
            result = await common.create_notification(
                notification_type,
                77,
                data if data is not None else {"item_name": "Bones", "npc_name": "Barrows"},
                group_id,
                existing_session=db,
            )
        return result, db

    async def test_blacklisted_subject_enqueues_nothing(self, common):
        result, db = await self._create(common, "blacklisted item 'bones'")
        assert result is None
        db.add.assert_not_called()

    async def test_unblacklisted_subject_still_enqueues(self, common):
        _, db = await self._create(common, None)
        assert db.add.called

    @pytest.mark.parametrize("notification_type", sorted(BLACKLISTABLE_TYPES))
    async def test_every_submission_type_is_covered(self, common, notification_type):
        _, db = await self._create(
            common, "blacklisted NPC 'barrows'", notification_type=notification_type
        )
        db.add.assert_not_called()

    async def test_personal_dm_survives_a_group_blacklist(self, common):
        # dm_* notifications carry no group_id, so no group's list can reach
        # them — same guarantee hiding a member already makes.
        db = MagicMock()
        with patch.object(common, "group_has_notification_channel", return_value=True), \
             patch.object(common, "player_hidden_for_group", return_value=False), \
             patch.object(common, "NotificationQueue", MagicMock()):
            common.stored_notifications = {}
            await common.create_notification(
                "dm_drop", 77, {"item_name": "Bones", "npc_name": "Barrows"},
                None, existing_session=db,
            )
        assert db.add.called


class TestNotificationBlacklistedHelper:
    """The thin wrapper create_notification calls (fail-open + group scoping)."""

    @pytest.fixture
    def common(self):
        import data.submissions.common as common

        return common

    def test_delegates_to_the_shared_matcher(self, common):
        db = _db([("item", "bones")])
        assert common.notification_blacklisted(
            db, 303, "drop", {"item_name": "Bones"}
        ) == "blacklisted item 'bones'"

    def test_no_group_id_short_circuits(self, common):
        db = _db([("item", "bones")])
        assert common.notification_blacklisted(db, None, "drop", {"item_name": "Bones"}) is None

    def test_lookup_failure_fails_open(self, common):
        db = _db([("item", "bones")], raises=True)
        assert common.notification_blacklisted(db, 303, "drop", {"item_name": "Bones"}) is None
