"""Unit tests for utils/npc_names.py — the NPC spelling/article/alias
identity rule (suggestion #50). Pure functions, no DB."""
from utils.npc_names import (
    MODE_SUFFIXES,
    npc_base_slug,
    npc_family_tiers,
    npc_match_key,
    npc_match_variants,
    npc_slug,
    npc_slug_sql_expr,
    strip_the,
)


def test_spelling_variants_share_a_slug():
    # The real-world splits that motivated the fix.
    assert (
        npc_slug("Chambers of Xeric (Challenge mode)")
        == npc_slug("Chambers of Xeric Challenge Mode")
        == "chambers-of-xeric-challenge-mode"
    )
    assert (
        npc_slug("Tombs of Amascut: Expert Mode")
        == npc_slug("Tombs of Amascut Expert Mode")
        == "tombs-of-amascut-expert-mode"
    )
    assert npc_slug("Ice Troll") == npc_slug("Ice troll") == "ice-troll"


def test_slug_edges():
    assert npc_slug(None) == ""
    assert npc_slug("!!!") == ""
    assert npc_slug("  Spaced  Out  ") == "spaced-out"


def test_the_article_is_insignificant_in_match_keys():
    assert strip_the("the-whisperer") == "whisperer"
    assert strip_the("whisperer") == "whisperer"
    assert strip_the("the-") == "the-"  # degenerate, not an article
    assert npc_match_key("The Whisperer") == npc_match_key("Whisperer") == "whisperer"
    assert npc_match_key("The Hueycoatl") == npc_match_key("Hueycoatl")
    assert npc_match_key("The Nightmare") == npc_match_key("Nightmare")
    assert npc_match_key("The Leviathan") == npc_match_key("Leviathan")
    assert npc_match_key("The Mimic") == npc_match_key("Mimic")


def test_aliases_map_boss_npc_to_activity():
    assert npc_match_key("Crystalline Hunllef") == npc_match_key("The Gauntlet") == "gauntlet"
    assert (
        npc_match_key("Corrupted Hunllef")
        == npc_match_key("The Corrupted Gauntlet")
        == npc_match_key("Corrupted Gauntlet")
        == "corrupted-gauntlet"
    )
    # Aliases are NOT symmetric name soup — unrelated bosses stay distinct.
    assert npc_match_key("Vorkath") == "vorkath"


def test_match_variants_cover_all_spellings():
    v = set(npc_match_variants("The Gauntlet"))
    assert {"gauntlet", "the-gauntlet", "crystalline-hunllef", "the-crystalline-hunllef"} <= v
    # Resolving FROM the alias name yields the same variant set.
    assert set(npc_match_variants("Crystalline Hunllef")) == v
    assert npc_match_variants("") == []
    assert npc_match_variants(None) == []


def test_base_slug_strips_mode_suffixes():
    assert npc_base_slug("tombs-of-amascut-expert-mode") == "tombs-of-amascut"
    assert npc_base_slug("chambers-of-xeric-challenge-mode") == "chambers-of-xeric"
    assert npc_base_slug("theatre-of-blood-hard-mode") == "theatre-of-blood"
    assert npc_base_slug("vorkath") is None
    assert npc_base_slug("hard-mode") is None  # bare suffix isn't a variant


def test_family_tiers_priority_order():
    tiers = npc_family_tiers("Chambers of Xeric Challenge Mode")
    # Tier 1: self variants; tier 2: base raid; tier 3: mode siblings.
    assert "chambers-of-xeric-challenge-mode" in tiers[0]
    assert "chambers-of-xeric" in tiers[1]
    assert any("hard-mode" in slug for slug in tiers[2])

    # A base raid: self first, then every mode sibling.
    tiers_base = npc_family_tiers("Theatre of Blood")
    assert "theatre-of-blood" in tiers_base[0]
    assert all(
        any(f"theatre-of-blood-{s}" in v for v in tiers_base[1])
        for s in MODE_SUFFIXES
    )

    # Alias family: The Gauntlet's tier 1 includes the Hunllef spellings.
    g = npc_family_tiers("Crystalline Hunllef")
    assert "the-gauntlet" in g[0] and "crystalline-hunllef" in g[0]

    assert npc_family_tiers("") == []


def test_sanitize_team_size():
    from utils.npc_names import sanitize_team_size as t

    # Every dirty encoding observed in personal_best (2026-07).
    assert t("(2") == "2"
    assert t("(2 players)") == "2"
    assert t("5 s") == "5"
    assert t("5 scaled") == "5"
    assert t("11-15 s") == "11-15"
    assert t("24+ s") == "24+"
    assert t("6+ s") == "6+"
    assert t("0") == "Solo"
    assert t(1) == "Solo"
    assert t("solo") == "Solo"
    assert t(None) == "Solo"
    # Canonical values are fixed points.
    for v in ("Solo", "2", "8", "11-15", "16-23", "24+", "6+"):
        assert t(v) == v


def test_sql_expr_matches_python_rule():
    expr = npc_slug_sql_expr("npc_name")
    assert "REGEXP_REPLACE" in expr and "LOWER(npc_name)" in expr
    assert "[^a-z0-9]+" in expr
