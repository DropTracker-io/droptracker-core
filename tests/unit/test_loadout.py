"""Decoding of the PB loadout wire format.

The encoded string comes straight from a game client, so the interesting cases
are the malformed ones: a bad loadout must degrade to "no loadout", never to a
wrong loadout and never to an exception that would take the personal best with
it.
"""
from services.loadout import (
    MAX_SLOTS,
    MODEL_SOURCE_KILL,
    MODEL_SOURCE_RECENT,
    loadout_from_json,
    parse_loadout,
    resolve_model_fingerprint,
    serialize_loadout,
)


class TestParseLoadout:
    def test_decodes_slots_in_order(self):
        assert parse_loadout("3-995-1000,0-11802-1") == [
            {"slot": 0, "item_id": 11802, "quantity": 1},
            {"slot": 3, "item_id": 995, "quantity": 1000},
        ]

    def test_empty_and_non_string_inputs(self):
        assert parse_loadout("") == []
        assert parse_loadout(None) == []
        assert parse_loadout(12345) == []
        assert parse_loadout({"slot": 1}) == []

    def test_skips_malformed_entries_but_keeps_good_ones(self):
        assert parse_loadout("0-11802-1,garbage,2-3,4-abc-1,5-995-10") == [
            {"slot": 0, "item_id": 11802, "quantity": 1},
            {"slot": 5, "item_id": 995, "quantity": 10},
        ]

    def test_rejects_zero_and_negative_ids_and_quantities(self):
        assert parse_loadout("0-0-1,1-995-0,2--5-1") == []

    def test_rejects_implausible_values(self):
        assert parse_loadout("9999-995-1") == []      # slot index far too high
        assert parse_loadout("0-99999999-1") == []    # item id far too high

    def test_duplicate_slot_keeps_the_first(self):
        assert parse_loadout("0-995-1,0-4151-1") == [
            {"slot": 0, "item_id": 995, "quantity": 1}
        ]

    def test_caps_the_number_of_slots(self):
        encoded = ",".join(f"{i}-995-1" for i in range(MAX_SLOTS + 20))
        assert len(parse_loadout(encoded)) == MAX_SLOTS

    def test_whitespace_tolerated(self):
        assert parse_loadout(" 0-995-1 , 1-4151-1 ") == [
            {"slot": 0, "item_id": 995, "quantity": 1},
            {"slot": 1, "item_id": 4151, "quantity": 1},
        ]


class TestSerialization:
    def test_round_trip(self):
        entries = parse_loadout("0-11802-1,3-995-1000")
        assert loadout_from_json(serialize_loadout(entries)) == entries

    def test_nothing_serializes_to_none_not_empty_array(self):
        """None and "[]" must stay distinguishable: one means the client sent no
        loadout, the other would mean the player was carrying nothing."""
        assert serialize_loadout([]) is None

    def test_unreadable_stored_value_is_empty(self):
        assert loadout_from_json("{not json") == []
        assert loadout_from_json(None) == []
        assert loadout_from_json('{"not": "a list"}') == []

    def test_stored_entries_with_wrong_types_are_dropped(self):
        raw = '[{"slot": 0, "item_id": 995, "quantity": 1}, {"slot": "x", "item_id": 1, "quantity": 1}]'
        assert loadout_from_json(raw) == [{"slot": 0, "item_id": 995, "quantity": 1}]


def test_a_full_inventory_fits_the_embed_field_limit():
    """The encoding exists because embed field values are capped at 1024 chars.
    A worst-case inventory has to fit, or loadouts would silently vanish for the
    players carrying the most interesting ones."""
    encoded = ",".join(f"{i}-{20000 + i}-2147483647" for i in range(28))
    assert len(encoded) < 1024
    assert len(parse_loadout(encoded)) == 28


class TestResolveModelFingerprint:
    """Which character model a personal best is rendered with, and why (web110a)."""

    def test_the_clients_own_fingerprint_wins_and_is_exact(self):
        assert resolve_model_fingerprint("2f3ab1c", "deadbeef") == ("2f3ab1c", MODEL_SOURCE_KILL)

    def test_without_one_the_servers_recent_outfit_stands_in_labelled_as_such(self):
        assert resolve_model_fingerprint(None, "deadbeef") == ("deadbeef", MODEL_SOURCE_RECENT)

    def test_nothing_usable_means_no_model_not_a_guess(self):
        assert resolve_model_fingerprint(None, None) == (None, None)
        assert resolve_model_fingerprint("", "") == (None, None)

    def test_a_hostile_value_never_becomes_a_fingerprint(self):
        # A fingerprint ends up in a storage key and a URL path, so anything
        # that is not plain hex is refused — from either side.
        for bad in ("../../etc", "abcdefg!", "a" * 33, "abc def", {"fp": "abcd"}, 1.5, True):
            assert resolve_model_fingerprint(bad, None) == (None, None)
        assert resolve_model_fingerprint(None, "../x") == (None, None)

    def test_case_and_whitespace_are_normalised(self):
        assert resolve_model_fingerprint("  1A2B3C  ", None) == ("1a2b3c", MODEL_SOURCE_KILL)

    def test_an_all_digit_fingerprint_survives_the_form_transports_int_conversion(self):
        # The form transport turns "31337" into 31337 on the way in; a Java
        # Integer.toHexString can be exactly that.
        assert resolve_model_fingerprint(31337, None) == ("31337", MODEL_SOURCE_KILL)
