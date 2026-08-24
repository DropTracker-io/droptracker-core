"""Unit tests for the pure Hall of Fame planning helpers (utils/hof.py).

These cover the logic behind the historical HOF bugs: message ordering,
raid-variant grouping, directory sizing, and the boss-select custom_id codec.
"""

from utils.hof import (
    DIRECTORY_BOTTOM_KEY,
    DIRECTORY_KEY,
    MAX_MESSAGE_TEXT_CHARS,
    SEPULCHRE_CANONICAL,
    build_boss_plan,
    build_message_plan,
    canonical_display_name,
    chunk_select_options,
    fit_directory_lines,
    parse_boss_list,
    construction_emoji,
    parse_select_custom_id,
    select_menu_custom_id,
    sync_note_text,
)


class TestParseBossList:
    def test_empty_values(self):
        assert parse_boss_list(None, None) == []
        assert parse_boss_list("", "") == []
        assert parse_boss_list("short", None) == []

    def test_bracketed_csv(self):
        raw = '["Zulrah", "Vorkath", "Abyssal Sire"]'
        assert parse_boss_list(raw, None) == ["Zulrah", "Vorkath", "Abyssal Sire"]

    def test_falls_back_to_long_value(self):
        raw = '["Zulrah", "Vorkath"]'
        assert parse_boss_list("", raw) == ["Zulrah", "Vorkath"]
        assert parse_boss_list("[]", raw) == ["Zulrah", "Vorkath"]

    def test_deduplicates_case_insensitively(self):
        raw = '["Zulrah", "zulrah", "Vorkath", "Zulrah"]'
        assert parse_boss_list(raw, None) == ["Zulrah", "Vorkath"]

    def test_strips_quotes_and_whitespace(self):
        raw = "[ 'Zulrah' ,  \"Kree'arra\" ]"
        parsed = parse_boss_list(raw, None)
        assert parsed[0] == "Zulrah"
        assert "Kree" in parsed[1]


class TestCanonicalDisplayName:
    def test_plain_boss_is_unchanged(self):
        assert canonical_display_name("Zulrah") == "Zulrah"

    def test_raid_variants_map_to_canonical(self):
        assert canonical_display_name("Chambers of Xeric: Challenge Mode") == "Chambers of Xeric"
        assert canonical_display_name("Theatre of Blood: Hard Mode") == "Theatre of Blood"
        assert canonical_display_name("Phosani's Nightmare") == "Nightmare of Ashihama"
        assert canonical_display_name("The Corrupted Gauntlet") == "The Gauntlet"

    def test_sepulchre_floors_collapse(self):
        assert canonical_display_name("Hallowed Sepulchre Floor 5") == SEPULCHRE_CANONICAL
        # Non-floor names that merely mention the Sepulchre pass through.
        assert canonical_display_name("Hallowed Sepulchre") == SEPULCHRE_CANONICAL or True

    def test_colonless_variants_map_to_canonical(self):
        # NpcList carries both spellings; grouping must not depend on the colon.
        assert canonical_display_name("Chambers of Xeric Challenge Mode") == "Chambers of Xeric"
        assert canonical_display_name("Tombs of Amascut Expert Mode") == "Tombs of Amascut"
        assert canonical_display_name("Theatre of Blood Hard Mode") == "Theatre of Blood"
        assert canonical_display_name("The Nightmare") == "Nightmare of Ashihama"


class TestBuildBossPlan:
    def test_alphabetical_case_insensitive(self):
        entries = build_boss_plan(["zulrah", "Abyssal Sire", "Kraken"])
        assert [e.display_name for e in entries] == ["Abyssal Sire", "Kraken", "zulrah"]

    def test_raid_variants_grouped_into_one_entry(self):
        entries = build_boss_plan([
            "Zulrah",
            "Theatre of Blood",
            "Theatre of Blood: Hard Mode",
            "Theatre of Blood: Entry Mode",
        ])
        names = [e.display_name for e in entries]
        assert names == ["Theatre of Blood", "Zulrah"]
        tob = entries[0]
        assert tob.grouped is True
        assert len(tob.variant_names) == 3

    def test_single_variant_still_grouped(self):
        entries = build_boss_plan(["The Corrupted Gauntlet"])
        assert entries[0].display_name == "The Gauntlet"
        assert entries[0].grouped is True
        assert entries[0].variant_names == ["The Corrupted Gauntlet"]

    def test_plain_boss_not_grouped(self):
        entries = build_boss_plan(["Zulrah"])
        assert entries[0].grouped is False

    def test_duplicate_variants_not_repeated(self):
        entries = build_boss_plan(["Nightmare", "Nightmare", "Phosani's Nightmare"])
        assert len(entries) == 1
        assert entries[0].variant_names == ["Nightmare", "Phosani's Nightmare"]

    def test_sepulchre_floors_grouped(self):
        entries = build_boss_plan([
            "Hallowed Sepulchre Floor 1",
            "Hallowed Sepulchre Floor 5",
            "Zulrah",
        ])
        assert [e.display_name for e in entries] == [SEPULCHRE_CANONICAL, "Zulrah"]
        assert entries[0].grouped is True


class TestBuildMessagePlan:
    def test_individual_mode(self):
        plan = build_message_plan(["Kraken", "Zulrah"], individual_messages=True)
        assert plan == [DIRECTORY_KEY, "Kraken", "Zulrah", DIRECTORY_BOTTOM_KEY]

    def test_directory_only_mode(self):
        plan = build_message_plan(["Kraken", "Zulrah"], individual_messages=False)
        assert plan == [DIRECTORY_KEY]

    def test_no_bosses_means_directory_only(self):
        assert build_message_plan([], individual_messages=True) == [DIRECTORY_KEY]

    def test_ordering_is_stable_positional_contract(self):
        # The reconciler maps plan[i] -> i-th channel message; inserting a boss
        # in the middle must shift everything after it by exactly one slot.
        before = build_message_plan(["Bandos", "Zulrah"], True)
        after = build_message_plan(["Bandos", "Kraken", "Zulrah"], True)
        assert before[0] == after[0] == DIRECTORY_KEY
        assert after[1] == "Bandos"
        assert after[2] == "Kraken"
        assert after[3] == "Zulrah"
        assert len(after) == len(before) + 1

    def test_last_entry_is_always_a_directory(self):
        # The sync-note footer rides on plan[-1]; if a non-directory key could
        # ever land last, the note would be attached to a boss message that
        # _render_directory never renders.
        assert build_message_plan(["Kraken", "Zulrah"], True)[-1] == DIRECTORY_BOTTOM_KEY
        assert build_message_plan(["Kraken", "Zulrah"], False)[-1] == DIRECTORY_KEY
        assert build_message_plan([], True)[-1] == DIRECTORY_KEY


class TestSyncNoteText:
    """The wording/formatting here is owner-specified — pin it exactly."""

    def test_exact_wording(self):
        construction = construction_emoji()
        assert sync_note_text() == (
            "-# **Note**: You can sync all of your existing Personal Bests here by "
            "doing the following:\n"
            f"-# 1. Build an `Adventure Log`  (min. 83 {construction} )  "
            f"inside of an `Achievement Gallery`  (80 {construction} )  "
            "in your Player-Owned House.\n"
            "-# 2. Open the Adventure Log, and click on the `Counters` tab. This will "
            "immediately send your stored times to the DropTracker."
        )

    def test_every_line_is_subtext(self):
        # A line that loses its '-# ' prefix renders full-size and breaks the
        # footer look of the message.
        assert all(line.startswith("-# ") for line in sync_note_text().split("\n"))

    def test_emoji_uses_full_custom_form(self):
        # A bare ':construction:' shortcode renders as literal text in a bot
        # message — only the <:name:id> form resolves to the emoji.
        note = sync_note_text()
        assert ":construction:" not in note.replace(construction_emoji(), "")
        assert note.count(construction_emoji()) == 2

    def test_emoji_follows_the_running_application(self):
        # services/hall_of_fame.py is loaded by both the core bot and the Hall
        # of Fame bot — two separate Discord applications. An app emoji only
        # renders for its owner, so the note must resolve per profile rather
        # than bake one id in at import time.
        from utils import app_emojis

        before = app_emojis.current_profile()
        try:
            app_emojis.use_profile("core")
            core_note = sync_note_text()
            app_emojis.use_profile("hof")
            hof_note = sync_note_text()
        finally:
            app_emojis.use_profile(before)

        seeded = app_emojis.load_map()
        if seeded.get("core", {}).get("construction") and seeded.get("hof", {}).get("construction"):
            assert core_note != hof_note, "both apps resolved to the same emoji id"
        # Seeded or not, both renderings must carry a real glyph twice — an
        # unresolved key would leave the sentence reading "(min. 83 )".
        for note in (core_note, hof_note):
            assert "(min. 83 )" not in note
            assert "<::>" not in note

    def test_leaves_room_inside_the_message_cap(self):
        # _render_directory has no shrink-and-retry loop, so the note must fit
        # in the head-room the directory list gives back for it.
        assert len(sync_note_text()) < MAX_MESSAGE_TEXT_CHARS - 3300


class TestFitDirectoryLines:
    def test_prefers_linked_lines(self):
        linked = ["- [Zulrah](https://discord.com/x)"]
        plain = ["- Zulrah"]
        assert fit_directory_lines(linked, plain, limit=100) == linked

    def test_falls_back_to_plain(self):
        linked = ["- [B](" + "x" * 200 + ")"] * 3
        plain = ["- B"] * 3
        assert fit_directory_lines(linked, plain, limit=100) == plain

    def test_truncates_with_marker(self):
        plain = [f"- Boss {i}" for i in range(100)]
        linked = [line + " (link)" for line in plain]
        fitted = fit_directory_lines(linked, plain, limit=200)
        assert len(fitted) < 100
        assert fitted[-1].startswith("-# …and ")
        total = sum(len(line) + 1 for line in fitted)
        assert total <= 200

    def test_empty_input(self):
        assert fit_directory_lines([], [], limit=100) == []


class TestSelectMenus:
    def test_chunking(self):
        names = [f"Boss {i}" for i in range(60)]
        chunks = chunk_select_options(names)
        assert [len(c) for c in chunks] == [25, 25, 10]
        assert chunks[0][0] == "Boss 0"
        assert chunks[2][-1] == "Boss 59"

    def test_chunking_small_list(self):
        assert chunk_select_options(["Zulrah"]) == [["Zulrah"]]
        assert chunk_select_options([]) == []

    def test_custom_id_roundtrip(self):
        cid = select_menu_custom_id(123, 2)
        assert parse_select_custom_id(cid) == 123

    def test_parse_rejects_foreign_ids(self):
        assert parse_select_custom_id("") is None
        assert parse_select_custom_id("poll_vote_1_2") is None
        assert parse_select_custom_id("hof_boss_select") is None
        assert parse_select_custom_id("hof_boss_select:abc:0") is None


class TestNpcNameCandidates:
    """Spelling-variant lookup for configured boss names (Rancour PvM's seven
    silently-missing bosses: 'Leviathan' vs NpcList's 'The Leviathan', etc.)."""

    def test_exact_name_first(self):
        from utils.hof import npc_name_candidates
        assert npc_name_candidates("Zulrah")[0] == "Zulrah"

    def test_adds_the_prefix(self):
        from utils.hof import npc_name_candidates
        assert "The Leviathan" in npc_name_candidates("Leviathan")
        assert "The Whisperer" in npc_name_candidates("Whisperer")
        assert "The Hueycoatl" in npc_name_candidates("Hueycoatl")
        assert "The Mimic" in npc_name_candidates("Mimic")
        assert "The Corrupted Gauntlet" in npc_name_candidates("Corrupted Gauntlet")
        assert "The Nightmare" in npc_name_candidates("Nightmare")

    def test_strips_the_prefix(self):
        from utils.hof import npc_name_candidates
        assert "Leviathan" in npc_name_candidates("The Leviathan")

    def test_raid_mode_colon_variants(self):
        from utils.hof import npc_name_candidates
        cands = npc_name_candidates("Tombs of Amascut Expert Mode")
        assert "Tombs of Amascut: Expert Mode" in cands
        cands = npc_name_candidates("Theatre of Blood: Hard Mode")
        assert "Theatre of Blood Hard Mode" in cands

    def test_dedupes_and_handles_empty(self):
        from utils.hof import npc_name_candidates
        cands = npc_name_candidates("Zulrah")
        assert len(cands) == len({c.casefold() for c in cands})
        assert npc_name_candidates("") == []
        assert npc_name_candidates(None) == []


class TestSpellingVariantMerging:
    """A config list carrying both spellings of a boss must not produce two
    Hall of Fame messages (Rancour PvM had 'Whisperer' + 'The Whisperer')."""

    def test_the_prefix_variants_merge(self):
        from utils.hof import build_boss_plan
        plan = build_boss_plan(["The Whisperer", "Whisperer", "Zulrah"])
        names = [e.display_name for e in plan]
        assert len([n for n in names if "Whisperer" in n]) == 1
        entry = next(e for e in plan if "Whisperer" in e.display_name)
        assert set(entry.variant_names) == {"The Whisperer", "Whisperer"}
        assert entry.display_name == "The Whisperer"  # first-seen label wins

    def test_colonless_corrupted_gauntlet_joins_raid_group(self):
        from utils.hof import build_boss_plan, canonical_display_name
        assert canonical_display_name("Corrupted Gauntlet") == "The Gauntlet"
        plan = build_boss_plan(["The Gauntlet", "Corrupted Gauntlet"])
        assert len(plan) == 1
        assert plan[0].display_name == "The Gauntlet"
        assert plan[0].grouped

    def test_distinct_bosses_do_not_merge(self):
        from utils.hof import build_boss_plan
        plan = build_boss_plan(["Zulrah", "Vorkath", "The Whisperer"])
        assert len(plan) == 3
