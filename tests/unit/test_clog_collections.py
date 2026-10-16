"""{collection_name}: the collection log page an item sits on (ticket #476)."""

from utils.clog_collections import collection_for_item


class TestCollectionForItem:
    def test_by_item_id(self):
        assert collection_for_item(4151) == "Abyssal Sire"  # Abyssal whip

    def test_by_name_when_the_id_is_missing(self):
        assert collection_for_item(None, "Hydra's claw") == "Alchemical Hydra"

    def test_name_lookup_ignores_case_and_spaces(self):
        assert collection_for_item("", "  hydra's CLAW ") == "Alchemical Hydra"

    def test_drawn_replacement_id_resolves(self):
        # Enum 3721: stored as 13273, drawn and reported as 25624 (Unsired);
        # the page lists the drawn id.
        assert collection_for_item(25624) == collection_for_item(13273) == "Abyssal Sire"

    def test_source_picks_among_several_pages(self):
        # Dragon pickaxe is on several boss pages; the named source wins.
        pages = {
            collection_for_item(11920, source=src)
            for src in ("Kalphite Queen", "Venenatis", "Chaos Elemental")
        }
        assert pages == {"Kalphite Queen", "Venenatis and Spindel", "Chaos Elemental"}

    def test_unmatched_source_falls_back_to_first_page(self):
        assert collection_for_item(11920, source="Clan Broadcast") == collection_for_item(11920)

    def test_not_a_slot(self):
        assert collection_for_item(995, "Coins") is None

    def test_bad_input(self):
        assert collection_for_item("abc") is None
        assert collection_for_item() is None
