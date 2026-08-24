"""Unit tests for utils/npc_names.py — the NPC spelling/article/alias
identity rule (suggestion #50). Pure functions, no DB."""
from utils.npc_names import (
    ENCOUNTER_NAME_ALIASES,
    MODE_SUFFIXES,
    MULTI_PATH_LOOT_SOURCES,
    canonical_encounter_name,
    is_multi_path_loot_source,
    npc_base_slug,
    npc_family_tiers,
    npc_match_key,
    npc_match_variants,
    npc_primary_rank_sql_expr,
    npc_primary_variants,
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


def test_encounter_members_rewrite_to_the_encounter_name():
    # The Hunllefs and the Titans have npc_list rows of their own, so an
    # exact-name lookup would store their PBs/drops on the boss id while the
    # loot path uses the activity id — two boards for one encounter.
    assert canonical_encounter_name("Crystalline Hunllef") == "The Gauntlet"
    assert canonical_encounter_name("Corrupted Hunllef") == "The Corrupted Gauntlet"
    assert canonical_encounter_name("Branda the Fire Queen") == "Royal Titans"
    # Sources disagree on capitalisation/spacing; the rewrite is slug-based.
    assert canonical_encounter_name("corrupted hunllef") == "The Corrupted Gauntlet"
    assert canonical_encounter_name("  Crystalline  Hunllef ") == "The Gauntlet"
    # Grotesque Guardians: loot drops from Dusk, so RuneLite's NpcLootReceived
    # names the guardian while LootReceived names the encounter. Both have
    # npc_list rows (7851 / 13960), so every GG kill split across two ids until
    # this was added (2026-08-03).
    assert canonical_encounter_name("Dusk") == "Grotesque Guardians"
    # Non-members and empties pass through untouched.
    assert canonical_encounter_name("Vorkath") == "Vorkath"
    assert canonical_encounter_name("The Gauntlet") == "The Gauntlet"
    assert canonical_encounter_name("") == ""
    assert canonical_encounter_name(None) is None


def test_multi_path_loot_sources():
    # Encounters whose loot reaches intake through more than one RuneLite loot
    # event, so an identical bundle twice in a row is ONE kill, not two.
    assert is_multi_path_loot_source("Grotesque Guardians")
    # ...addressed by either name, and spelling-insensitively.
    assert is_multi_path_loot_source("Dusk")
    assert is_multi_path_loot_source("grotesque guardians")
    assert is_multi_path_loot_source("Branda the Fire Queen")
    assert is_multi_path_loot_source("Crystalline Hunllef")
    assert is_multi_path_loot_source("Araxxor")
    # Ordinary NPCs must NOT be — AoE slayer legitimately produces two
    # identical bundles in one tick and both are real.
    assert not is_multi_path_loot_source("Abyssal demon")
    assert not is_multi_path_loot_source("Maniacal monkey")
    assert not is_multi_path_loot_source("Vorkath")
    # Raids have their own (much longer) re-loot window, not this one.
    assert not is_multi_path_loot_source("Theatre of Blood")
    assert not is_multi_path_loot_source("")
    assert not is_multi_path_loot_source(None)


def test_every_multi_path_source_is_its_own_canonical_name():
    # is_multi_path_loot_source canonicalises before matching, so a set member
    # that is itself an alias would be unreachable.
    for name in MULTI_PATH_LOOT_SOURCES:
        assert canonical_encounter_name(name) == name
        assert is_multi_path_loot_source(name)


def test_encounter_aliases_resolve_to_real_encounters():
    # A typo'd target would silently mint a second npc_list row instead of
    # folding onto the encounter's.
    for member, encounter in ENCOUNTER_NAME_ALIASES.items():
        assert canonical_encounter_name(member) == encounter
        assert canonical_encounter_name(encounter) == encounter


def test_primary_variants_exclude_alias_spellings():
    # The tie-break set for "which npc_list row IS this boss": the canonical
    # spellings only. Crystalline Hunllef (id 9021) sorts below The Gauntlet
    # (13703) despite the lower id precisely because it is not in here.
    assert npc_primary_variants("The Gauntlet") == ["gauntlet", "the-gauntlet"]
    # Resolving FROM the alias name yields the canonical primaries, not its own.
    assert npc_primary_variants("Crystalline Hunllef") == ["gauntlet", "the-gauntlet"]
    assert npc_primary_variants("Corrupted Gauntlet") == [
        "corrupted-gauntlet",
        "the-corrupted-gauntlet",
    ]
    for name in ("The Gauntlet", "Crystalline Hunllef", "Royal Titans"):
        assert set(npc_primary_variants(name)) < set(npc_match_variants(name))
    assert npc_primary_variants("") == []
    assert npc_primary_variants(None) == []


def test_primary_rank_sql_expr_shape():
    expr = npc_primary_rank_sql_expr("n.npc_name")
    # Canonical spellings sort first (0), aliases after (1).
    assert expr.startswith("(CASE WHEN ") and "THEN 0 ELSE 1 END)" in expr
    assert npc_slug_sql_expr("n.npc_name") in expr
    assert ":primary_variants" in expr
    assert ":other" in npc_primary_rank_sql_expr("npc_name", param="other")


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


def test_team_size_cap_is_the_games_own_party_ceiling():
    from utils.npc_names import team_size_cap

    # Five health orbs / eight health orbs — the raid cannot hold more.
    assert team_size_cap("Theatre of Blood") == 5
    assert team_size_cap("Tombs of Amascut") == 8
    # Mode variants inherit the base raid's ceiling.
    assert team_size_cap("Theatre of Blood: Hard Mode") == 5
    assert team_size_cap("Theatre of Blood: Entry Mode") == 5
    assert team_size_cap("Tombs of Amascut: Expert Mode") == 8
    assert team_size_cap("Tombs of Amascut: Entry Mode") == 8
    # Spelling-insensitive, like every other lookup in this module.
    assert team_size_cap("theatre of blood hard mode") == 5
    # Chambers of Xeric masses legitimately; nothing else is capped.
    assert team_size_cap("Chambers of Xeric") is None
    assert team_size_cap("Chambers of Xeric Challenge Mode") is None
    assert team_size_cap("Vorkath") is None
    assert team_size_cap(None) is None
    assert team_size_cap("") is None


def test_clamp_team_size_rejects_impossible_raid_teams():
    """Suggestion #140: a contaminated client roster submitted Theatre of Blood
    times as 6-, 7-, 8- and 9-player raids, and the Hall of Fame rendered a
    board per bucket."""
    from utils.npc_names import clamp_team_size as c

    assert c("Theatre of Blood", "9") == "5"
    assert c("Theatre of Blood", "6") == "5"
    assert c("Theatre of Blood: Hard Mode", 8) == "5"
    assert c("Tombs of Amascut: Expert Mode", "15") == "8"
    # A bracket is over the cap when its lowest member already is.
    assert c("Theatre of Blood", "6+") == "5"
    assert c("Theatre of Blood", "11-15") == "5"

    # Real sizes pass through untouched, cap included.
    for size in ("Solo", "2", "3", "4", "5"):
        assert c("Theatre of Blood", size) == size
    assert c("Tombs of Amascut", "8") == "8"

    # Uncapped bosses keep every bracket they legitimately produce.
    assert c("Chambers of Xeric", "24+") == "24+"
    assert c("Chambers of Xeric", "11-15") == "11-15"
    assert c("The Nightmare", "6") == "6"

    # Still does sanitize_team_size' job on the way through.
    assert c("Theatre of Blood", "(3 players)") == "3"
    assert c("Theatre of Blood", "0") == "Solo"
    assert c("Theatre of Blood", None) == "Solo"
    assert c(None, "9") == "9"


def test_sql_expr_matches_python_rule():
    expr = npc_slug_sql_expr("npc_name")
    assert "REGEXP_REPLACE" in expr and "LOWER(npc_name)" in expr
    assert "[^a-z0-9]+" in expr
