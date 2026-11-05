"""{item_rarity}: the wiki drop rate of a notified item (utils/drop_rarity.py)."""
import pytest

from utils import drop_rarity
from utils.drop_rarity import RarityTable, format_rate, parse_quantity, rarity_text
from utils.format import replace_placeholders
from utils.game_emojis import with_derived_tokens

from tests.unit.test_format import _StubEmbed, _StubField

# (npc_id, npc_name, item_name, quantity, rarity, rolls), shaped like the
# xenforo.dt_npc_loot rows the loader reads.
ROWS = [
    (1, "Night beast", "Eternal gem", "1", 1 / 256, 1),
    # A wiki revision repeat of the same line.
    (1, "Night beast", "Eternal gem", "1", 1 / 256, 1),
    (2, "Zulrah", "Tanzanite fang", "1", 1 / 1024, 2),
    (3, "Vorkath", "Vorki", "1", 1 / 3000, 1),
    (4, "Vorkath", "Vorki", "1", 1 / 3000, 1),
    (5, "Chambers of Xeric", "Twisted bow", "1", 1 / 30, 1),
    (13, "Clue Scroll (Easy)", "Bob's green shirt", "1", 1 / 1404, 1),
    (6, "Yama", "Oathplate chest", "1", 1 / 600, 1),
    (6, "Yama", "Oathplate chest", "1", 1.0, 1),
    (7, "Fortis Colosseum", "Sunfire fanatic helm", "1", 1 / 620, 1),
    (7, "Fortis Colosseum", "Sunfire fanatic helm", "1", 1 / 64, 1),
    (8, "Araxyte", "Uncut diamond", "1", 1 / 2048, 1),
    (8, "Araxyte", "Uncut diamond", "1", 0.5, 3),
    (9, "Ice troll", "Adamant kiteshield", "1", 1 / 128, 1),
    (10, "Ice troll", "Adamant kiteshield", "1", 1 / 16, 1),
    (11, "Mummy", "Coins", "36", 0.04, 1),
    (11, "Mummy", "Coins", "472", 0.0195, 1),
    (12, "Rune dragon", "Dragon limbs", "1", 1 / 5000, 1),
    (12, "Rune dragon", "Runite bar", "2-4 (noted)", 1 / 20, 1),
]


@pytest.fixture
def table():
    t = RarityTable(ROWS)
    drop_rarity.set_table(t)
    yield t
    drop_rarity.set_table(None)


class TestParseQuantity:
    def test_single(self):
        assert parse_quantity("1") == ((1, 1),)

    def test_range_noted(self):
        assert parse_quantity("2-4 (noted)") == ((2, 4),)

    def test_en_dash(self):
        assert parse_quantity("5–10") == ((5, 10),)

    def test_alternatives(self):
        assert parse_quantity("1, 3") == ((1, 1), (3, 3))

    def test_thousands_separator_is_one_number(self):
        assert parse_quantity("1,000") == ((1000, 1000),)

    def test_garbage(self):
        assert parse_quantity("N/A") == ()


class TestFormatRate:
    def test_the_dink_example(self):
        assert format_rate(1 / 256) == "1 in 256 (0.391%)"

    def test_thousands(self):
        assert format_rate(1 / 3000) == "1 in 3,000 (0.0333%)"

    def test_common(self):
        assert format_rate(1 / 12.5) == "1 in 12.5 (8%)"

    def test_very_common(self):
        assert format_rate(0.5) == "1 in 2 (50%)"

    def test_tiny(self):
        assert format_rate(1e-6) == "1 in 1,000,000 (<0.001%)"

    @pytest.mark.parametrize("p", [None, 0, 1, 1.5, -0.1])
    def test_no_rate(self, p):
        assert format_rate(p) is None


class TestRate:
    def test_by_npc_id(self, table):
        assert table.rate("Eternal gem", "Night beast", 1) == pytest.approx(1 / 256)

    def test_by_name_only(self, table):
        assert table.rate("Eternal gem", "Night beast") == pytest.approx(1 / 256)

    def test_item_name_is_case_insensitive(self, table):
        assert table.rate("eternal GEM", "night beast") == pytest.approx(1 / 256)

    def test_multi_roll_table_is_per_kill(self, table):
        assert table.rate("Tanzanite fang", "Zulrah", 2) == pytest.approx(1 - (1 - 1 / 1024) ** 2)

    def test_unknown_id_falls_back_to_name(self, table):
        assert table.rate("Vorki", "Vorkath", 999) == pytest.approx(1 / 3000)

    def test_raid_odds_are_conditional_and_skipped(self, table):
        assert table.rate("Twisted bow", "Chambers of Xeric", 5) is None
        assert table.rate("Twisted bow", "Chambers of Xeric Challenge Mode") is None

    def test_clue_casket_rates_are_per_roll_and_skipped(self, table):
        assert table.rate("Bob's green shirt", "Clue Scroll (Easy)") is None

    def test_always_line_is_ignored(self, table):
        assert table.rate("Oathplate chest", "Yama", 6) == pytest.approx(1 / 600)

    def test_conflicting_lines_give_nothing(self, table):
        assert table.rate("Sunfire fanatic helm", "Fortis Colosseum", 7) is None

    def test_multi_roll_sub_table_line_is_dropped(self, table):
        assert table.rate("Uncut diamond", "Araxyte", 8) == pytest.approx(1 / 2048)

    def test_npc_variants_that_disagree_give_nothing(self, table):
        assert table.rate("Adamant kiteshield", "Ice troll") is None

    def test_npc_id_with_its_own_line_wins(self, table):
        assert table.rate("Adamant kiteshield", "Ice troll", 9) == pytest.approx(1 / 128)

    def test_quantity_picks_the_line(self, table):
        assert table.rate("Coins", "Mummy", 11, quantity=472) == pytest.approx(0.0195)

    def test_quantity_in_range(self, table):
        assert table.rate("Runite bar", "Rune dragon", 12, quantity=3) == pytest.approx(1 / 20)

    def test_no_quantity_and_lines_disagree(self, table):
        assert table.rate("Coins", "Mummy", 11) is None

    def test_unknown_item_or_npc(self, table):
        assert table.rate("Twisted bow", "Night beast", 1) is None
        assert table.rate("Eternal gem", "Goblin") is None
        assert table.rate("", "Night beast") is None


class TestRarityText:
    def test_text(self, table):
        assert rarity_text("Eternal gem", "Night beast", "1", "`1`") == "1 in 256 (0.391%)"

    def test_needs_a_source(self, table):
        assert rarity_text("Eternal gem") is None

    def test_load_failure_fails_open(self, monkeypatch):
        drop_rarity.set_table(None)

        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(drop_rarity, "_load_rows", boom)
        try:
            assert rarity_text("Eternal gem", "Night beast") is None
            # Failed recently: no retry storm on every notification.
            monkeypatch.setattr(drop_rarity, "_load_rows", lambda: pytest.fail("retried"))
            assert rarity_text("Eternal gem", "Night beast") is None
        finally:
            drop_rarity.set_table(None)

    def test_first_lookup_loads(self, monkeypatch):
        drop_rarity.set_table(None)
        monkeypatch.setattr(drop_rarity, "_load_rows", lambda: ROWS)
        try:
            assert rarity_text("Vorki", "Vorkath") == "1 in 3,000 (0.0333%)"
        finally:
            drop_rarity.set_table(None)


class TestDerivedToken:
    def test_drop_values_get_a_rarity(self, table):
        values = with_derived_tokens({"{item_name}": "Eternal gem", "{npc_name}": "Night beast",
                                      "{npc_id}": "1", "{quantity}": "`1`"})
        assert values["{item_rarity}"] == "1 in 256 (0.391%)"

    def test_clog_values_by_name(self, table):
        values = with_derived_tokens({"{item_name}": "Vorki", "{npc_name}": "Vorkath"})
        assert values["{item_rarity}"] == "1 in 3,000 (0.0333%)"

    def test_a_sender_value_is_kept(self, table):
        values = with_derived_tokens({"{item_name}": "Vorki", "{npc_name}": "Vorkath",
                                      "{item_rarity}": "custom"})
        assert values["{item_rarity}"] == "custom"

    def test_no_source_is_missing_not_blank(self, table):
        values = with_derived_tokens({"{item_name}": "Vorki", "{npc_name}": None})
        assert values["{item_rarity}"] is None

    def test_a_description_line_without_a_rate_drops(self, table):
        embed = _StubEmbed(title="{item_name}", description="G/E Value: `1M`\nDrop rate: `{item_rarity}`")
        out = replace_placeholders(embed, {"{item_name}": "Vorki", "{npc_name}": None})
        assert out.description == "G/E Value: `1M`"

    def test_not_derived_without_an_item(self, table):
        assert "{item_rarity}" not in with_derived_tokens({"{npc_name}": "Vorkath"})

    def test_the_field_drops_when_there_is_no_rate(self, table):
        embed = _StubEmbed(title="{item_name}")
        embed.fields = [_StubField("Item Rarity", "`{item_rarity}`"), _StubField("KC", "1")]
        out = replace_placeholders(embed, {"{item_name}": "Twisted bow",
                                           "{npc_name}": "Chambers of Xeric", "{npc_id}": "5"})
        assert [f.name for f in out.fields] == ["KC"]

    def test_the_field_shows_the_rate(self, table):
        embed = _StubEmbed(title="{item_name}")
        embed.fields = [_StubField("Item Rarity", "`{item_rarity}`")]
        out = replace_placeholders(embed, {"{item_name}": "Eternal gem",
                                           "{npc_name}": "Night beast", "{npc_id}": "1"})
        assert out.fields[0].value == "`1 in 256 (0.391%)`"


class TestDefaultLayouts:
    def _render(self, kind, values):
        from services.component_layout import default_layout, render_layout
        return str(render_layout(default_layout(kind), values))

    def test_drop_layout_shows_the_rate(self, table):
        out = self._render("drop", {"{item_name}": "Eternal gem", "{npc_name}": "Night beast",
                                    "{npc_id}": "1", "{item_value}": "`7.58M`", "{player_name}": "Ron"})
        assert "Drop rate: `1 in 256 (0.391%)`" in out

    @pytest.mark.parametrize("kind", ["drop", "clog"])
    def test_layout_line_drops_without_a_rate(self, table, kind):
        out = self._render(kind, {"{item_name}": "Twisted bow", "{npc_name}": "Chambers of Xeric",
                                  "{item_value}": "`1.3B`", "{kc_received}": "12", "{player_name}": "Ron"})
        assert "Drop rate" not in out
        assert "Twisted bow" in out
