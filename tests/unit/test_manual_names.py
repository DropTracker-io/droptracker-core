"""Typed item / NPC names on Discord manual submissions.

Both Discord surfaces hand the intake whatever the member typed, and the
intake matches names exactly, so a typo, a dropped apostrophe or an
abbreviation used to be a rejection. Names are now placed in the catalog
before anything is sent: one clear match is corrected (and the member is told
what it was recorded as), a few plausible ones come back as "did you mean?"
buttons, and a name nothing resembles goes through untouched.
"""
from __future__ import annotations

import json

import pytest

from data.submissions.manual_names import (
    ITEM_ALIASES,
    NAME_PICK_MAX,
    NPC_NAME_ALIASES,
    PICK_AS_TYPED,
    PICK_CANCEL,
    RESOLUTION_LOG_KEY,
    NameCatalog,
    correction_lines,
    fold,
    log_resolution,
    parse_pick,
    pick_components,
    question_text,
    resolve_fields,
    resolve_name,
    suggest_names,
)
from utils.npc_names import npc_match_key

_ITEM_ROWS = [
    ("Twisted bow", 1), ("Twisted buckler", 1), ("Dragon claws", 1),
    ("Abyssal whip", 1), ("Abyssal head", 1), ("Tumeken's shadow (uncharged)", 1),
    ("Tumeken's shadow", 1), ("Scythe of vitur (uncharged)", 1), ("Scythe of vitur", 1),
    ("Scythe", 0), ("Ancestral hat", 1), ("Ancestral robe top", 1),
    ("Ancestral robe bottom", 1), ("Elder maul", 1), ("Zaryte crossbow", 0),
    ("Coins", 1), ("Dragon warhammer", 1),
    # The same name twice (variant ids): the tracked row decides.
    ("Dragon pickaxe", 0), ("Dragon pickaxe", 1),
]
_NPC_ROWS = [
    ("Zulrah", 1), ("Vorkath", 1), ("Chambers of Xeric", 1),
    ("Chambers of Xeric Challenge Mode", 1), ("Theatre of Blood", 1),
    ("Theatre of Blood: Hard Mode", 1), ("The Gauntlet", 1),
    ("The Corrupted Gauntlet", 1), ("Duke Sucellus", 1), ("Duke", 0),
    ("Phosani's Nightmare", 1), ("The Nightmare", 1), ("Corporeal Beast", 1),
    ("Corpse", 1), ("Kree'arra", 1), ("Nex", 1), ("King Black Dragon", 1),
]


@pytest.fixture(scope="module")
def items():
    return NameCatalog(_ITEM_ROWS, ITEM_ALIASES)


@pytest.fixture(scope="module")
def npcs():
    return NameCatalog(_NPC_ROWS, NPC_NAME_ALIASES, npc_match_key)


class TestFold:
    @pytest.mark.parametrize("a, b", [
        ("Tumekens shadow", "Tumeken's shadow"),
        ("kree arra", "Kree-arra"),
        ("TWISTED  BOW", "twisted bow"),
        ("Scythe of vitur uncharged", "Scythe of vitur (uncharged)"),
    ])
    def test_spellings_meet(self, a, b):
        assert fold(a) == fold(b)


class TestResolveItems:
    @pytest.mark.parametrize("typed, expected", [
        ("Twisted bow", "Twisted bow"),
        ("twisted BOW", "Twisted bow"),
        ("tbow", "Twisted bow"),                 # abbreviation
        ("twistd bow", "Twisted bow"),           # typo
        ("abyssal whp", "Abyssal whip"),         # typo, clear of "Abyssal head"
        ("elder mual", "Elder maul"),
        ("Tumekens shadow", "Tumeken's shadow"),  # dropped apostrophe
        ("dragon claw", "Dragon claws"),         # the only name starting with it
        ("dwh", "Dragon warhammer"),
        ("Dragon pickaxe", "Dragon pickaxe"),
    ])
    def test_one_item(self, items, typed, expected):
        match, _candidates = resolve_name(typed, items)
        assert match == expected

    def test_several_matches_are_asked_about(self, items):
        match, candidates = resolve_name("ancestral", items)
        assert match is None
        assert set(candidates) == {"Ancestral hat", "Ancestral robe top",
                                   "Ancestral robe bottom"}

    def test_stray_catalog_row_does_not_win_over_the_real_item(self, items):
        # "Scythe" is a real row nobody has ever received. Typed without the
        # alias, the exact hit must not be taken on its own.
        bare = NameCatalog(_ITEM_ROWS)
        match, candidates = resolve_name("Scythe", bare)
        assert match is None
        assert candidates[0] == "Scythe"
        assert "Scythe of vitur" in candidates

    def test_untracked_exact_name_with_no_rival_is_taken(self, items):
        assert resolve_name("zaryte crossbow", items)[0] == "Zaryte crossbow"

    def test_nothing_close(self, items):
        assert resolve_name("xyzzy plugh", items) == (None, ())

    def test_empty(self, items):
        assert resolve_name("", items) == (None, ())
        assert resolve_name("   ", items) == (None, ())

    def test_no_catalog_leaves_the_name_alone(self):
        assert resolve_name("Twisted bow", None) == (None, ())
        assert resolve_name("Twisted bow", NameCatalog([])) == (None, ())

    def test_candidates_are_capped(self):
        rows = [(f"Rune thing {i}", 1) for i in range(20)]
        _match, candidates = resolve_name("rune thing", NameCatalog(rows))
        assert len(candidates) == NAME_PICK_MAX

    def test_close_runner_up_is_asked_not_guessed(self):
        catalog = NameCatalog([("Dragon dart", 1), ("Dragon dirt", 1)])
        match, candidates = resolve_name("dragon dort", catalog)
        assert match is None
        assert set(candidates) == {"Dragon dart", "Dragon dirt"}


class TestResolveNpcs:
    @pytest.mark.parametrize("typed, expected", [
        ("zulrah", "Zulrah"),
        ("Zulra", "Zulrah"),
        ("Vorkth", "Vorkath"),
        ("cox", "Chambers of Xeric"),
        ("tob", "Theatre of Blood"),
        ("corp", "Corporeal Beast"),             # not "Corpse"
        ("duke", "Duke Sucellus"),               # not the stray "Duke" row
        ("Duke Succellus", "Duke Sucellus"),
        ("gauntlet", "The Gauntlet"),            # npc_match_key drops the article
        ("phosanis nightmare", "Phosani's Nightmare"),
        ("kree arra", "Kree'arra"),
        ("nex", "Nex"),
    ])
    def test_one_npc(self, npcs, typed, expected):
        assert resolve_name(typed, npcs)[0] == expected

    def test_prefix_of_two_is_asked_about(self, npcs):
        match, candidates = resolve_name("Chambers", npcs)
        assert match is None
        assert candidates == ("Chambers of Xeric", "Chambers of Xeric Challenge Mode")

    def test_initials_are_offered_never_assumed(self):
        catalog = NameCatalog([("King Black Dragon", 1), ("Kalphite Queen", 1)])
        match, candidates = resolve_name("kbd", catalog)
        assert match is None
        assert candidates == ("King Black Dragon",)

    def test_alias_to_a_missing_target_is_ignored(self):
        catalog = NameCatalog([("Zulrah", 1)], {"cox": "Chambers of Xeric"})
        assert resolve_name("cox", catalog) == (None, ())


class TestSuggestNames:
    def test_resolved_name_alone(self, items):
        assert suggest_names("tbow", items) == ["Twisted bow"]

    def test_candidates(self, items):
        assert "Ancestral hat" in suggest_names("ancestral", items)

    def test_nothing(self, items):
        assert suggest_names("xyzzy plugh", items) == []


class TestResolveFields:
    def test_drop_corrects_both_and_reports_what_changed(self, items, npcs):
        fields, corrections, question = resolve_fields(
            "drop", {"item_name": "twistd bow", "npc_name": "cox", "quantity": 1},
            items=items, npcs=npcs)
        assert question is None
        assert fields == {"item_name": "Twisted bow", "npc_name": "Chambers of Xeric",
                          "quantity": 1}
        assert corrections == [("item_name", "twistd bow", "Twisted bow"),
                               ("npc_name", "cox", "Chambers of Xeric")]

    def test_case_only_change_is_not_a_correction(self, items, npcs):
        fields, corrections, question = resolve_fields(
            "drop", {"item_name": "twisted BOW", "npc_name": "zulrah"},
            items=items, npcs=npcs)
        assert (fields["item_name"], fields["npc_name"]) == ("Twisted bow", "Zulrah")
        assert corrections == [] and question is None

    def test_ambiguous_item_stops_before_anything_is_sent(self, items, npcs):
        _fields, _corrections, question = resolve_fields(
            "drop", {"item_name": "ancestral", "npc_name": "cox"}, items=items, npcs=npcs)
        assert question["field"] == "item_name"
        assert question["raw"] == "ancestral"
        assert "Ancestral hat" in question["candidates"]

    def test_confirmed_field_is_not_asked_again(self, items, npcs):
        fields, _corrections, question = resolve_fields(
            "drop", {"item_name": "ancestral", "npc_name": "Chambers"},
            items=items, npcs=npcs, confirmed={"item_name"})
        assert fields["item_name"] == "ancestral"
        assert question["field"] == "npc_name"

    def test_unknown_names_go_through_untouched(self, items, npcs):
        fields, corrections, question = resolve_fields(
            "drop", {"item_name": "Brand new item", "npc_name": "Brand new boss"},
            items=items, npcs=npcs)
        assert fields == {"item_name": "Brand new item", "npc_name": "Brand new boss"}
        assert corrections == [] and question is None

    def test_pet_name_is_left_to_the_pet_resolver(self, items, npcs):
        fields, _c, question = resolve_fields(
            "pet", {"item_name": "tbow", "npc_name": "Zulra"}, items=items, npcs=npcs)
        assert fields == {"item_name": "tbow", "npc_name": "Zulrah"}
        assert question is None

    def test_pb_boss(self, items, npcs):
        fields, _c, _q = resolve_fields(
            "pb", {"npc_name": "Vorkth", "time_ms": 60000}, items=None, npcs=npcs)
        assert fields["npc_name"] == "Vorkath"

    def test_missing_optional_source_and_ca_are_skipped(self, items, npcs):
        assert resolve_fields("clog", {"item_name": "tbow", "npc_name": None},
                              items=items, npcs=npcs)[0]["npc_name"] is None
        ca = {"task": "Perfect Zulrah", "tier": "elite"}
        assert resolve_fields("ca", ca, items=items, npcs=npcs) == (ca, [], None)


class TestCopy:
    def test_question_names_a_single_candidate(self):
        text = question_text({"raw": "Zulra", "label": "a boss or NPC",
                              "candidates": ("Zulrah",)})
        assert text.startswith("We don't have a boss or NPC called “Zulra”. Did you mean **Zulrah**?")
        assert "Nothing has been submitted yet" in text

    def test_question_with_several(self):
        text = question_text({"raw": "ancestral", "label": "an item",
                              "candidates": ("Ancestral hat", "Ancestral robe top")})
        assert "Pick the one you meant" in text

    def test_correction_line(self):
        assert correction_lines([("item_name", "tbow", "Twisted bow")]) == [
            "-# Recorded as **Twisted bow** (you typed “tbow”)."]


class TestPickButtons:
    def test_round_trip(self):
        rows = pick_components("tok_en-1", ["Ancestral hat", "Ancestral robe top"])
        picks, extras = rows[0]["components"], rows[1]["components"]
        assert [b["label"] for b in picks] == ["Ancestral hat", "Ancestral robe top"]
        assert [parse_pick(b["custom_id"]) for b in picks] == [("tok_en-1", 0), ("tok_en-1", 1)]
        assert [parse_pick(b["custom_id"]) for b in extras] == [
            ("tok_en-1", PICK_AS_TYPED), ("tok_en-1", PICK_CANCEL)]

    def test_no_as_typed_button_when_it_cannot_succeed(self):
        rows = pick_components("t", ["Olmlet"], allow_as_typed=False)
        assert [b["label"] for b in rows[1]["components"]] == ["Cancel"]

    def test_at_most_five_per_row_and_short_labels(self):
        rows = pick_components("t", [f"{'x' * 120}{i}" for i in range(9)])
        assert len(rows[0]["components"]) == NAME_PICK_MAX
        assert all(len(b["label"]) <= 80 for b in rows[0]["components"])

    @pytest.mark.parametrize("custom_id", [
        "dtsubmit:petpick:tok:0", "dtsubmit:namepick:", "dtsubmit:namepick:tok:zz",
        "dtsubmit:namepick::1", None, 5,
    ])
    def test_other_ids_are_not_ours(self, custom_id):
        assert parse_pick(custom_id) is None


class TestResolutionLog:
    def test_entry_is_pushed_and_capped(self):
        calls = []

        class Conn:
            def lpush(self, key, value):
                calls.append(("lpush", key, json.loads(value)))

            def ltrim(self, key, start, stop):
                calls.append(("ltrim", key, start, stop))

        log_resolution(Conn(), surface="panel", kind="npc_name", raw="Zulra",
                       outcome="auto", chosen="Zulrah")
        assert calls[0][1] == RESOLUTION_LOG_KEY
        assert calls[0][2]["raw"] == "Zulra" and calls[0][2]["chosen"] == "Zulrah"
        assert calls[1][:3] == ("ltrim", RESOLUTION_LOG_KEY, 0)

    def test_never_raises(self):
        class Broken:
            def lpush(self, *a):
                raise RuntimeError("redis down")

        log_resolution(None, surface="slash", kind="item_name", raw="x", outcome="asked")
        log_resolution(Broken(), surface="slash", kind="item_name", raw="x", outcome="asked")
