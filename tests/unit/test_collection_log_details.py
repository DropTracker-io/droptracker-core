"""Attaching a submission's date and screenshot to a collection log slot.

The join everyone writes first — ``collection.item_id == <slot id>`` — is wrong,
and wrong silently. The plugin does not know the slot id; it reads the item's
*name* out of the chat line and asks ``ItemIDSearch``, which answers with the
earliest cache id sharing that name. So the row for the log's "Coal bag" slot
25627 is filed under 764, "Crawling hand" 7975 under 4133, "Gem bag" 25628 under
766. Those slots would show no screenshot despite having one.

Matching on the name recovers them, and is the one thing here that can go
genuinely wrong: eighteen slots are called "Chompy bird hat" and twenty-six are
called "Ancient page", so a name that identifies more than one slot must resolve
to none of them. A slot with no screenshot is a small loss. A slot showing the
screenshot of a different unlock is a lie about what the player did.
"""

from __future__ import annotations

from datetime import datetime

import web_api.routes.player_state as ps

HOSTED = "https://www.droptracker.io/img/user-upload/1051950/clog/Yama/Oathplate_chest.png"
DISCORD = "https://cdn.discordapp.com/attachments/1/2/proof.png"

JAN1 = datetime(2026, 1, 1, 12, 0)
JAN2 = datetime(2026, 1, 2, 12, 0)
JAN3 = datetime(2026, 1, 3, 12, 0)


def details(rows, slot_ids, item_names, wiki_names=None):
    return ps.collection_details(rows, set(slot_ids), item_names, wiki_names or {})


class TestIdMatch:
    def test_a_row_filed_under_the_slots_own_id(self):
        got = details([(30753, JAN1, HOSTED)], [30753], {30753: "Oathplate chest"})
        assert got == {30753: {"ts": int(JAN1.timestamp()), "image_url": HOSTED}}

    def test_a_slot_with_no_row_is_absent(self):
        # The common case by a wide margin: a row exists only for an unlock the
        # plugin announced. Everything older came from the interface scrape,
        # which carries no date and no screenshot.
        assert details([], [30753, 25627], {30753: "Oathplate chest"}) == {}

    def test_a_row_for_an_item_the_log_does_not_define_is_dropped(self):
        # Pets and seasonal items reach this table too; only slots are answered.
        assert details([(1234, JAN1, HOSTED)], [30753], {1234: "Something else"}) == {}


class TestNameFallback:
    def test_recovers_the_ids_itemidsearch_gets_wrong(self):
        # Each pair here is a real divergence between what the plugin recorded
        # and what the log's slot id is.
        for plugin_id, slot_id, name in (
            (764, 25627, "Coal bag"),
            (4133, 7975, "Crawling hand"),
            (766, 25628, "Gem bag"),
        ):
            got = details(
                [(plugin_id, JAN1, HOSTED)],
                [slot_id],
                {plugin_id: name, slot_id: name},
            )
            assert got == {slot_id: {"ts": int(JAN1.timestamp()), "image_url": HOSTED}}

    def test_a_name_several_slots_share_resolves_to_none_of_them(self):
        # "Graceful hood" is slots 11850 and 21061. Picking either would put a
        # screenshot on a slot the player may never have filled.
        got = details(
            [(999999, JAN1, HOSTED)],
            [11850, 21061],
            {999999: "Graceful hood", 11850: "Graceful hood", 21061: "Graceful hood"},
        )
        assert got == {}

    def test_falls_back_to_the_wikis_name_for_a_slot_we_have_no_item_row_for(self):
        # A dozen slots have no `items` row, which is exactly why the structure
        # ships the wiki's names alongside the ids.
        got = details(
            [(29990, JAN1, HOSTED)],
            [29988],
            {29990: "Alchemist's amulet"},
            {29988: "Alchemist's amulet"},
        )
        assert 29988 in got

    def test_ignores_case_and_spacing_between_the_two_name_sources(self):
        got = details(
            [(1, JAN1, HOSTED)],
            [2],
            {1: "Bandos  d'hide boots", 2: "Bandos d'hide Boots"},
        )
        assert 2 in got

    def test_an_id_we_have_no_name_for_is_skipped(self):
        assert details([(4133, JAN1, HOSTED)], [7975], {7975: "Crawling hand"}) == {}

    def test_a_name_matching_nothing_is_skipped(self):
        got = details([(4133, JAN1, HOSTED)], [7975], {4133: "Crawling hand"})
        assert got == {}

    def test_name_rows_merge_into_the_slots_own_rows(self):
        # A player can end up with both spellings across plugin versions. The
        # date should still be the earliest of the two and the screenshot the
        # one that exists, whichever row it came from.
        got = details(
            [(25627, JAN1, None), (764, JAN2, HOSTED)],
            [25627],
            {764: "Coal bag", 25627: "Coal bag"},
        )
        assert got == {25627: {"ts": int(JAN1.timestamp()), "image_url": HOSTED}}


class TestProofUrls:
    def test_a_discord_cdn_link_is_not_offered(self):
        # Those links expire, so serving one gives the player a broken image
        # where the screenshot should be.
        got = details([(30753, JAN1, DISCORD)], [30753], {30753: "Oathplate chest"})
        assert got == {30753: {"ts": int(JAN1.timestamp()), "image_url": None}}

    def test_an_empty_url_is_not_offered(self):
        got = details([(30753, JAN1, "")], [30753], {30753: "Oathplate chest"})
        assert got[30753]["image_url"] is None


class TestSeveralRowsForOneItem:
    """Clog unlocks are not deduped — the ``notified`` table only guards drops —
    so a re-sent notification simply writes another row."""

    def test_the_date_is_the_earliest_row(self):
        got = details(
            [(30753, JAN3, None), (30753, JAN1, None), (30753, JAN2, None)],
            [30753],
            {30753: "Oathplate chest"},
        )
        assert got[30753]["ts"] == int(JAN1.timestamp())

    def test_the_screenshot_comes_from_the_earliest_row_that_has_one(self):
        # ~100 of production's ~1,400 duplicated player/item pairs have their
        # screenshot on a later row than the one that dates the unlock.
        got = details(
            [(30753, JAN1, None), (30753, JAN2, HOSTED)],
            [30753],
            {30753: "Oathplate chest"},
        )
        assert got == {30753: {"ts": int(JAN1.timestamp()), "image_url": HOSTED}}

    def test_an_undated_row_never_supplies_the_date(self):
        got = details(
            [(30753, None, None), (30753, JAN2, None)],
            [30753],
            {30753: "Oathplate chest"},
        )
        assert got[30753]["ts"] == int(JAN2.timestamp())

    def test_rows_saying_nothing_produce_no_entry(self):
        # No date and no usable screenshot is not worth a line in the payload;
        # the slot renders with its name alone, as an unobtained one does.
        assert details([(30753, None, DISCORD)], [30753], {30753: "Oathplate chest"}) == {}


class TestSlotIndex:
    def test_a_shared_name_is_marked_unusable_rather_than_dropped(self):
        index = ps._slot_by_name({11850, 21061}, {11850: "Graceful hood", 21061: "Graceful hood"}, {})
        assert index["graceful hood"] is None

    def test_both_spellings_of_one_slot_point_at_it(self):
        index = ps._slot_by_name({3140}, {3140: "Dragon chainbody"}, {3140: "Dragon  Chainbody"})
        assert index == {"dragon chainbody": 3140}
