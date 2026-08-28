"""Item names and stack-size sprite variants — `utils/item_catalogue.py`.

Both answers used to come from the ``items`` table, which is not a catalogue —
it only holds what somebody has submitted. That is why 89 of 95 gear ids on
personal-best loadouts had no row, and why a tooltip driven off it would read
"Item 30000" for exactly the items that already rendered wrong.

The stack thresholds matter for a subtler reason: the three hand-written copies
of the coin table this replaces all had 10 -> 1000, 50 -> 1001 and 100 -> 1002,
where the game switches at 25, 100 and 250. A stack of 100 coins was drawn with
the 250-coin pile.
"""
import json

import pytest

from utils import item_catalogue


@pytest.fixture(autouse=True)
def _isolated_catalogue(tmp_path, monkeypatch):
    """Point the module at a small fixture and clear its memo between tests."""
    path = tmp_path / "item_catalogue.json"
    path.write_text(
        json.dumps(
            {
                "items": {
                    "995": {
                        "n": "Coins",
                        "sv": [
                            [2, 996], [3, 997], [4, 998], [5, 999], [25, 1000],
                            [100, 1001], [250, 1002], [1000, 1003], [10000, 1004],
                        ],
                    },
                    "4151": {"n": "Abyssal whip"},
                    "142": {"n": "Prayer potion(2)"},
                }
            }
        )
    )
    monkeypatch.setattr(item_catalogue, "CATALOGUE_PATH", str(path))
    monkeypatch.setattr(item_catalogue, "_catalogue", None)
    monkeypatch.setattr(item_catalogue, "_loaded_mtime", None)
    return path


class TestNames:
    def test_names_an_item(self):
        assert item_catalogue.item_name(4151) == "Abyssal whip"

    def test_names_a_noted_item_the_cache_leaves_blank(self):
        # Noted items carry no name of their own; the extractor resolves them
        # through noteLinkedItem, and they are ~5% of real loadout ids.
        assert item_catalogue.item_name(142) == "Prayer potion(2)"

    def test_unknown_ids_are_absent_rather_than_guessed(self):
        assert item_catalogue.item_name(999999) is None
        assert item_catalogue.item_name(None) is None
        assert item_catalogue.item_name("nonsense") is None

    def test_bulk_lookup_skips_what_it_does_not_know(self):
        assert item_catalogue.item_names([4151, 999999]) == {4151: "Abyssal whip"}


class TestStackDisplayId:
    @pytest.mark.parametrize(
        "quantity,expected",
        [
            (1, 995),      # below the first threshold: the base sprite
            (2, 996),
            (24, 999),     # just under the 25 pile
            (25, 1000),
            (99, 1000),
            (100, 1001),   # the old hand-written table said 1002 here
            (250, 1002),
            (1000, 1003),
            (100_000, 1004),
        ],
    )
    def test_picks_the_pile_the_game_would_draw(self, quantity, expected):
        assert item_catalogue.stack_display_id(995, quantity) == expected

    def test_a_non_stackable_item_never_swaps(self):
        assert item_catalogue.stack_display_id(4151, 10_000) == 4151

    def test_an_unknown_item_is_returned_unchanged(self):
        # The panel must render *something*; an id we cannot resolve keeps its
        # own sprite rather than becoming a blank slot.
        assert item_catalogue.stack_display_id(999999, 5000) == 999999

    def test_bad_input_does_not_raise(self):
        assert item_catalogue.stack_display_id(None, 5) is None
        assert item_catalogue.stack_display_id(995, None) == 995


class TestLargestStackId:
    def test_returns_the_fullest_pile(self):
        # The loot boards want the fullest pile regardless of quantity, which is
        # a different policy from stack_display_id but the same thresholds.
        assert item_catalogue.largest_stack_id(995) == 1004

    def test_none_for_items_with_no_variants(self):
        assert item_catalogue.largest_stack_id(4151) is None
        assert item_catalogue.largest_stack_id(999999) is None


class TestStackVariantIds:
    def test_collects_every_variant_for_the_given_bases(self):
        assert item_catalogue.stack_variant_ids([995]) == {
            996, 997, 998, 999, 1000, 1001, 1002, 1003, 1004
        }

    def test_ignores_bases_with_no_variants(self):
        assert item_catalogue.stack_variant_ids([4151, 999999]) == set()

    def test_whole_catalogue_when_asked_for_everything(self):
        # This is what the icon sweep uses: a pile sprite is an id the site can
        # render that nothing has ever submitted, so nothing else fetches it.
        assert 1004 in item_catalogue.stack_variant_ids()


class TestMissingCatalogue:
    def test_a_box_without_a_catalogue_degrades_quietly(self, monkeypatch):
        # Callers all have their own fallbacks; an empty catalogue is a valid
        # "we do not know" and must not take the API down.
        monkeypatch.setattr(item_catalogue, "CATALOGUE_PATH", "/nonexistent/x.json")
        monkeypatch.setattr(item_catalogue, "_catalogue", None)
        monkeypatch.setattr(item_catalogue, "_loaded_mtime", None)
        assert item_catalogue.item_name(995) is None
        assert item_catalogue.stack_display_id(995, 10_000) == 995
        assert item_catalogue.stack_variant_ids() == set()
