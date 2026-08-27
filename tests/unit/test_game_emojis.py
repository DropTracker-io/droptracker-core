"""Item and NPC glyphs: the set is a budget, and every miss is a normal miss.

``utils/game_emojis.py`` differs from the UI and rank sets in two ways that need
pinning, because both are easy to undo by accident and neither fails loudly:

* **It is deliberately incomplete.** 29k items exist and ~1000 have a glyph, so
  "not in the set" is the common answer, not an error. Every lookup has to
  return None for it — a raise here would drop a whole notification, and a
  ``<::>`` would show the reader a broken reference.
* **Items and NPCs share a namespace of keys but not of emoji.** "Unsired",
  "Bird nest" and "Intricate pouch" are each an item *and* a clog source, so
  anything keyed on the entity key alone hands one of them the other's art.

Plus the property that spans all three sets on this one application: their name
prefixes must stay disjoint, or one seeder's ``--prune`` deletes another's work.
"""
import json
import re
from pathlib import Path

import pytest

from tests.local_artifacts import skip_without
from utils import game_emojis
from utils.game_emojis import (
    ITEM_PREFIX,
    MANIFEST_PATH,
    MAP_PATH,
    MAX_EMOJI_NAME,
    NPC_PREFIX,
    PROFILE_TOKENS,
    emoji_for_item,
    emoji_for_item_id,
    emoji_for_npc,
    emoji_for_npc_id,
    emoji_name,
    is_valid_emoji_name,
    item_key,
    load_manifest,
    load_map,
    manifest_entries,
    npc_key,
    prefix_item,
    prefix_npc,
    validate_manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``<:name:id>`` as a message embeds it.
_REFERENCE = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>")

#: Discord's per-application ceiling, shared by all three sets.
APP_EMOJI_LIMIT = 2000


@pytest.fixture
def restore_profile():
    """Profile is process-global state; put it back however the test ends."""
    before = game_emojis.current_profile()
    yield
    game_emojis.use_profile(before)


def _manifest():
    manifest = load_manifest()
    if not manifest["items"] and not manifest["npcs"]:
        pytest.skip("data/game_emojis.json has not been generated on this checkout")
    return manifest


class TestKeys:
    def test_item_key_folds_spelling_and_punctuation(self):
        for spelling in ("Zenyte shard", "zenyte  shard", " ZENYTE SHARD "):
            assert item_key(spelling) == "zenyte_shard"

    def test_npc_key_folds_articles_and_aliases(self):
        # utils.npc_names is the codebase's one NPC identity rule; this set has
        # to agree with it or a PB notification and a drop notification for the
        # same boss resolve to different glyphs.
        assert npc_key("The Gauntlet") == npc_key("Crystalline Hunllef") == "gauntlet"
        assert npc_key("Chambers of Xeric (Challenge mode)") == \
            npc_key("Chambers of Xeric Challenge Mode")

    def test_blank_names_do_not_produce_a_key(self):
        for blank in (None, "", "   ", "!!!"):
            assert item_key(blank) == ""
            assert npc_key(blank) == ""


class TestEmojiNames:
    def test_names_are_prefixed_by_kind(self):
        assert emoji_name("item", "Twisted bow").startswith(ITEM_PREFIX)
        assert emoji_name("npc", "Zulrah").startswith(NPC_PREFIX)

    def test_an_item_and_an_npc_of_the_same_name_do_not_collide(self):
        # "Unsired" is both. Without the prefixes they are one name, and the
        # second upload silently takes over the first entity's glyph.
        assert emoji_name("item", "Unsired") != emoji_name("npc", "Unsired")

    def test_long_names_are_clamped_to_discords_limit(self):
        for name in ("Rune scimitar ornament kit (saradomin)",
                     "Perfected quetzal whistle blueprint",
                     "Charge dragonstone jewellery scroll"):
            assert len(emoji_name("item", name)) <= MAX_EMOJI_NAME
            assert is_valid_emoji_name(emoji_name("item", name))

    def test_a_trailing_qualifier_survives_truncation(self):
        # OSRS puts the distinguishing word last, so naive truncation makes the
        # three god variants of one ornament kit indistinguishable.
        names = {emoji_name("item", f"Rune scimitar ornament kit ({god})")
                 for god in ("saradomin", "guthix", "zamorak")}
        assert len(names) == 3, names
        for name in names:
            assert len(name) <= MAX_EMOJI_NAME

    def test_generated_names_are_all_acceptable_to_discord(self):
        for entry in manifest_entries():
            assert is_valid_emoji_name(entry["emoji"]), entry


class TestManifest:
    def test_manifest_is_internally_consistent(self):
        _manifest()
        # Valid names, and no name or key claimed twice. Whether the art is on
        # disk is a fact about the machine, not the manifest — TestArtIsPresent
        # covers that, on the machines that can answer it.
        assert validate_manifest(check_art=False) == []

    def test_manifest_fits_inside_the_application_ceiling(self):
        manifest = _manifest()
        total = len(manifest["items"]) + len(manifest["npcs"])
        # 270 rank_* + 8 UI keys are already up on the core app.
        assert total + 278 <= APP_EMOJI_LIMIT, (
            f"{total} entries plus the rank and UI sets exceeds Discord's "
            f"{APP_EMOJI_LIMIT} per application"
        )

    def test_every_entry_names_the_kind_the_section_implies(self):
        manifest = _manifest()
        for entry in manifest["items"]:
            assert entry["key"] == item_key(entry["name"]), entry
        for entry in manifest["npcs"]:
            assert entry["key"] == npc_key(entry["name"]), entry

    def test_art_id_is_one_of_the_ids_the_entry_claims(self):
        for entry in manifest_entries():
            assert entry["id"] in entry["ids"], entry

    def test_manifest_records_how_it_was_built(self):
        # The criteria block is what makes a regenerated set comparable to this
        # one; without it a changed cut-off is invisible in review.
        criteria = _manifest().get("criteria") or {}
        for field in ("budget", "min_drop_groups", "measured_fanout"):
            assert field in criteria, criteria


class TestLookup:
    def test_an_entity_outside_the_set_is_none_not_an_exception(self):
        # The common case: the set covers ~1000 of 29k items. Message-building
        # code calls this, so a raise would cost a whole notification.
        assert emoji_for_item("Not A Real Item") is None
        assert emoji_for_npc("Not A Real Boss") is None
        assert emoji_for_item_id(-1) is None
        assert emoji_for_npc_id(None) is None
        assert emoji_for_item(None) is None

    def test_unseeded_profile_resolves_nothing(self, restore_profile):
        _manifest()
        game_emojis.use_profile("no-such-app")
        for entry in manifest_entries()[:25]:
            assert emoji_for_item(entry["name"]) is None or entry["kind"] == "npc"

    def test_prefix_helpers_degrade_to_the_bare_name(self, restore_profile):
        game_emojis.use_profile("no-such-app")
        assert prefix_item("Twisted bow") == "Twisted bow"
        assert prefix_npc("Zulrah") == "Zulrah"
        assert prefix_item(None) == ""

    def test_a_seeded_profile_resolves_to_a_real_reference(self, restore_profile):
        seeded = load_map().get("core") or {}
        if not seeded:
            pytest.skip("static/game_emojis.json has no core profile yet")
        game_emojis.use_profile("core")
        for kind, lookup in (("item", emoji_for_item), ("npc", emoji_for_npc)):
            entries = {e["key"]: e for e in load_manifest()[f"{kind}s"]}
            for key in list(seeded.get(kind, {}))[:50]:
                reference = lookup(entries[key]["name"])
                assert reference == seeded[kind][key]
                assert _REFERENCE.fullmatch(reference), (kind, key, reference)

    def test_every_id_an_entry_claims_finds_the_same_glyph(self, restore_profile):
        # Noted and placeholder ids share a name and must share the glyph, or a
        # noted drop renders blank while the un-noted one renders.
        seeded = load_map().get("core") or {}
        if not seeded.get("item"):
            pytest.skip("static/game_emojis.json has no seeded items yet")
        game_emojis.use_profile("core")
        multi = [e for e in load_manifest()["items"]
                 if len(e["ids"]) > 1 and e["key"] in seeded["item"]]
        if not multi:
            pytest.skip("no seeded item folds more than one id")
        for entry in multi[:25]:
            references = {emoji_for_item_id(i) for i in entry["ids"]}
            assert references == {seeded["item"][entry["key"]]}, entry

    def test_npc_spelling_variants_find_one_glyph(self, restore_profile):
        seeded = (load_map().get("core") or {}).get("npc") or {}
        if "zulrah" not in seeded:
            pytest.skip("Zulrah is not seeded on this checkout")
        game_emojis.use_profile("core")
        for spelling in ("Zulrah", "zulrah", "  ZULRAH  "):
            assert emoji_for_npc(spelling) == seeded["zulrah"]


class TestMapFile:
    def test_map_is_keyed_by_profile_then_kind_then_key(self):
        if not Path(MAP_PATH).exists():
            pytest.skip("not seeded on this checkout")
        data = json.loads(Path(MAP_PATH).read_text(encoding="utf-8"))
        assert data, "seeded map is empty"
        manifest = {kind: {e["key"] for e in load_manifest()[f"{kind}s"]}
                    for kind in ("item", "npc")}
        for profile, kinds in data.items():
            assert set(kinds) <= {"item", "npc"}, profile
            for kind, entries in kinds.items():
                for key, reference in entries.items():
                    assert key in manifest[kind], f"{profile}.{kind}.{key} is not in the manifest"
                    assert _REFERENCE.fullmatch(reference), f"{profile}.{kind}.{key}={reference!r}"

    def test_a_key_only_in_the_map_does_not_resolve(self, restore_profile):
        # Half-finished state is normal: the manifest is regenerated in one
        # commit and the app is reseeded later. The honest answer is no glyph.
        game_emojis.use_profile("core")
        original = game_emojis._map_cache["map"]
        try:
            game_emojis._map_cache["map"] = {
                "core": {"item": {"definitely_not_in_the_manifest": "<:x:123456789012345678>"}}
            }
            assert emoji_for_item("definitely not in the manifest") is None
        finally:
            game_emojis._map_cache["map"] = original

    def test_profiles_name_a_token_variable_each(self):
        for profile, variable in PROFILE_TOKENS.items():
            assert variable.endswith("TOKEN"), (profile, variable)
        assert game_emojis.DEFAULT_PROFILE in PROFILE_TOKENS


class TestSeedersDoNotDeleteEachOther:
    """Three sets share one application, and each seeder has a ``--prune``.

    Prune decides "not mine" from a name prefix, so a set whose prefix another
    seeder does not know about is deleted wholesale on the next run of that
    seeder — 1000 uploads lost to a flag that reads as housekeeping.
    """

    def test_the_three_namespaces_are_disjoint(self):
        assert ITEM_PREFIX != NPC_PREFIX
        for prefix in (ITEM_PREFIX, NPC_PREFIX):
            assert not prefix.startswith("rank_")
            assert not "rank_".startswith(prefix)

    def test_the_ui_seeder_excludes_the_item_and_npc_prefixes(self):
        source = (REPO_ROOT / "scripts" / "seed_app_emojis.py").read_text(encoding="utf-8")
        assert "OTHER_SET_PREFIXES" in source
        for prefix in (ITEM_PREFIX, NPC_PREFIX, "rank_"):
            assert f'"{prefix}"' in source, (
                f"scripts/seed_app_emojis.py --prune does not know about {prefix!r} "
                "and will delete that whole set"
            )

    def test_the_rank_seeder_only_prunes_its_own_prefix(self):
        source = (REPO_ROOT / "scripts" / "seed_rank_emojis.py").read_text(encoding="utf-8")
        assert 'n.startswith("rank_")' in source, (
            "seed_rank_emojis.py --prune must positively select rank_* rather "
            "than deleting everything it does not recognise"
        )

    def test_no_manifest_entry_squats_another_sets_prefix(self):
        for entry in manifest_entries():
            assert not entry["emoji"].startswith("rank_"), entry
            expected = ITEM_PREFIX if entry["kind"] == "item" else NPC_PREFIX
            assert entry["emoji"].startswith(expected), entry


class TestArtIsPresent:
    def test_every_entry_has_art_on_disk(self):
        _manifest()
        skip_without(
            game_emojis.art_is_available(),
            "the item and NPC art under static/assets/img",
            "Populate it with scripts/rank_game_emojis.py --write.",
        )
        missing = [e["name"] for e in manifest_entries()
                   if not game_emojis.art_path_for(e).exists()]
        assert not missing, (
            f"{len(missing)} entries have no art and cannot be seeded; rerun "
            f"scripts/rank_game_emojis.py --write. First few: {missing[:5]}"
        )

    def test_manifest_path_is_committed_data_not_generated_static(self):
        # The manifest is a decision (reviewable in a diff); the map is an
        # upload receipt. Keeping them in one place would make a reseed look
        # like a change to the set.
        assert MANIFEST_PATH.parent.name == "data"
        assert Path(MAP_PATH).parent.name == "static"
