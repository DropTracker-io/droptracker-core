"""Clan Log section resolution for the task generator catalog.

The generator's uniques and pets come from the Clan Log catalog. It once named
sections by slug, and the v2 catalog reseed (2026-10-19) renamed every slug,
which silently left 44 of 45 bosses with no unique tasks ("it only gives me
Corp"). Sections are now matched on ``npc_keys``; these tests pin that.
"""
from web_api.task_generator_catalog import _json_keys, _sections_for

SLUGS_BY_KEY = {
    "zulrah": ["bosses_zulrah"],
    "gauntlet": ["bosses_the_gauntlet"],
    "barrows": ["group_bosses_ahrim", "group_bosses_dharok"],
    "vardorvis": ["desert_treasure_vardorvis"],
}


def test_matches_on_the_bosses_own_npc_key_whatever_the_slug():
    enc = {"kc_npcs": ["Zulrah"]}
    assert _sections_for(enc, SLUGS_BY_KEY) == ["bosses_zulrah"]


def test_one_npc_key_can_claim_several_sections():
    enc = {"kc_npcs": ["Barrows"]}
    assert _sections_for(enc, SLUGS_BY_KEY) == ["group_bosses_ahrim", "group_bosses_dharok"]


def test_clog_keys_and_explicit_sections_add_to_the_match():
    cg = {"kc_npcs": ["The Corrupted Gauntlet"], "clog_keys": ["gauntlet"]}
    assert _sections_for(cg, SLUGS_BY_KEY) == ["bosses_the_gauntlet"]
    vard = {"kc_npcs": ["Vardorvis"], "sections": ["desert_treasure_virtus_set"]}
    assert _sections_for(vard, SLUGS_BY_KEY) == ["desert_treasure_vardorvis",
                                                 "desert_treasure_virtus_set"]


def test_npc_keys_json_is_read_defensively():
    assert _json_keys('["a", "b"]') == ["a", "b"]
    assert _json_keys(None) == []
    assert _json_keys("not json") == []
    assert _json_keys('{"a": 1}') == []
