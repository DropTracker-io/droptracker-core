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


from utils.clog_collections import completed_page, notify_mode

# Royal Titans, in log order: Bran, the two prayer scrolls, Giantsoul amulet,
# the two staff crowns, Desiccated page.
TITANS = [30622, 30626, 30627, 30637, 30628, 30631, 30640]


class _Session:
    """Returns ``held`` as the player's filled slots, whatever the query."""

    def __init__(self, held):
        self.held = held
        self.calls = 0

    def execute(self, *_args, **_kwargs):
        self.calls += 1
        held = self.held

        class _Result:
            def fetchall(self):
                return [(i,) for i in held]

        return _Result()


class TestCompletedPage:
    """Suggestion #238: post only the slot that completes a page."""

    def test_last_slot_completes_the_page(self):
        assert completed_page(_Session(TITANS[:-1]), 1, TITANS[-1], "Desiccated page") == "Royal Titans"

    def test_a_missing_slot_means_not_complete(self):
        assert completed_page(_Session(TITANS[1:-1]), 1, TITANS[-1], "Desiccated page") is None

    def test_the_new_slot_counts_before_the_sync_catches_up(self):
        # Matched by name when the submission carries an id the page does not list.
        assert completed_page(_Session(TITANS[:-1]), 1, 999999, "desiccated page") == "Royal Titans"

    def test_drawn_and_stored_ids_both_count(self):
        # Unsired is drawn as 25624 but can be stored as 13273.
        held = [13262, 13273, 7979, 13274, 13275, 13276, 13277, 13265]
        assert completed_page(_Session(held), 1, 4151, "Abyssal whip") == "Abyssal Sire"

    def test_not_a_slot_never_queries(self):
        session = _Session([])
        assert completed_page(session, 1, 995, "Coins") is None
        assert session.calls == 0

    def test_mode_defaults_to_all(self):
        assert notify_mode(None) == "all"
        assert notify_mode("bogus") == "all"
        assert notify_mode(" Completed ") == "completed"
