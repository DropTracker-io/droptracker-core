"""Team chat-tag derivation (services/event_teams.py, web103a).

Pure logic: the tags the plugin prints beside a teammate's name in clan chat.
Loaded directly from the file path because the conftest stubs the ``services``
package; the module's own imports are stdlib-only, and the one lazy import
(the Discord orb map) is exercised in its own class.
"""

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(module_name, *path_parts):
    path = os.path.join(_ROOT, *path_parts)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


et = _load("_event_teams_under_test", "services", "event_teams.py")
etd = _load("_event_team_discord_for_teams_test", "services", "event_team_discord.py")


@pytest.fixture
def real_team_discord():
    """Let ``team_badge``'s lazy ``services.event_team_discord`` import resolve.

    The conftest replaces ``services`` with a MagicMock, so the real module is
    registered under its dotted path — and removed again straight afterwards.
    Leaving it there would give every later test in the session a REAL
    event_team_discord where it expects the stub, and the route tests that
    call into it against a fake session then fail (the b2_storage poisoning
    pattern).
    """
    key = "services.event_team_discord"
    previous = sys.modules.get(key)
    sys.modules[key] = etd
    try:
        yield etd
    finally:
        if previous is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = previous


class FakeTeam:
    def __init__(self, id, name, short_tag=None, color=None, piece_item_id=None):
        self.id = id
        self.name = name
        self.short_tag = short_tag
        self.color = color
        self.piece_item_id = piece_item_id


class TestDeriveShortTag:
    def test_multi_word_gives_initials(self):
        assert et.derive_short_tag("Red Rockets") == "RR"
        assert et.derive_short_tag("Blue Team") == "BT"

    def test_initials_are_capped(self):
        assert et.derive_short_tag("The Sunday Night Regulars Of Lumbridge") == "TSNR"

    def test_single_word_gives_a_prefix(self):
        assert et.derive_short_tag("Vanguard") == "VAN"

    def test_short_single_word_is_not_padded(self):
        assert et.derive_short_tag("Ox") == "OX"

    def test_hyphens_and_underscores_break_words(self):
        assert et.derive_short_tag("Red-Rockets") == "RR"
        assert et.derive_short_tag("red_rockets") == "RR"

    def test_punctuation_is_not_an_initial(self):
        assert et.derive_short_tag("Zezima's Crew") == "ZC"

    def test_digits_count_as_word_characters(self):
        assert et.derive_short_tag("Team 1") == "T1"

    def test_unusable_names_still_produce_something(self):
        # A tag has to be printable — the alternative is a team with no badge
        # at all, which reads as "not in the event".
        assert et.derive_short_tag("") == "T"
        assert et.derive_short_tag(None) == "T"
        assert et.derive_short_tag("δοκιμή") == "T"

    def test_is_pure(self):
        assert et.derive_short_tag("Red Rockets") == et.derive_short_tag("Red Rockets")

    def test_never_exceeds_the_column(self):
        for name in ["A B C D E F G H", "Supercalifragilistic", "x" * 200]:
            assert len(et.derive_short_tag(name)) <= et.SHORT_TAG_MAX


class TestSanitizeShortTag:
    def test_strips_what_chat_cannot_draw(self):
        assert et.sanitize_short_tag("RR★") == "RR"

    def test_collapses_whitespace(self):
        assert et.sanitize_short_tag("  R  R  ") == "R R"

    def test_truncates_to_the_column(self):
        assert et.sanitize_short_tag("ABCDEFGHIJ") == "ABCDEFGH"

    def test_empty_after_cleaning_is_unset(self):
        # Same signal as a NULL column: derive one instead.
        assert et.sanitize_short_tag("★★") is None
        assert et.sanitize_short_tag("   ") is None
        assert et.sanitize_short_tag(None) is None


class TestAssignShortTags:
    def test_admin_override_wins(self):
        teams = [FakeTeam(1, "Red Rockets", short_tag="ROCK")]
        assert et.assign_short_tags(teams) == {1: "ROCK"}

    def test_falls_back_to_derivation(self):
        assert et.assign_short_tags([FakeTeam(1, "Red Rockets")]) == {1: "RR"}

    def test_collisions_are_broken(self):
        # Two teams printing the same badge is worse than no badge: the reader
        # draws a confident wrong conclusion.
        tags = et.assign_short_tags([FakeTeam(1, "Red Rockets"), FakeTeam(2, "Red Ravens")])
        assert tags == {1: "RR", 2: "RR2"}

    def test_collision_counter_keeps_going(self):
        tags = et.assign_short_tags([
            FakeTeam(1, "Red Rockets"), FakeTeam(2, "Red Ravens"), FakeTeam(3, "Rapid Riders"),
        ])
        assert len(set(tags.values())) == 3

    def test_collision_suffix_respects_the_column(self):
        teams = [FakeTeam(i, "Alphabetic") for i in range(1, 4)]
        tags = et.assign_short_tags(teams)
        assert all(len(t) <= et.SHORT_TAG_MAX for t in tags.values())
        assert len(set(tags.values())) == 3

    def test_collision_is_case_insensitive(self):
        # The badge is read, not parsed: "rr" and "RR" are the same thing to
        # someone glancing at a chat line.
        tags = et.assign_short_tags([
            FakeTeam(1, "x", short_tag="rr"), FakeTeam(2, "Red Rockets"),
        ])
        assert tags[1].lower() != tags[2].lower()

    def test_stable_for_the_same_input(self):
        teams = [FakeTeam(1, "Red Rockets"), FakeTeam(2, "Blue Blazers")]
        assert et.assign_short_tags(teams) == et.assign_short_tags(teams)

    def test_unusable_names_do_not_collapse_together(self):
        tags = et.assign_short_tags([FakeTeam(1, ""), FakeTeam(2, "")])
        assert tags[1] != tags[2]


class TestTeamBadge:
    def test_orb_matches_the_discord_channel_icon(self, real_team_discord):
        # The whole point of routing through event_team_discord: the badge in
        # game is the circle the team's Discord channel already carries.
        team = FakeTeam(1, "Blue Blazers", color="#3355cc")
        badge = et.team_badge(team, 0)
        assert badge["orb"] == etd.team_channel_icon("#3355cc", 0)
        assert badge["orb_color"] == etd.ORB_COLORS[badge["orb"]]

    def test_carries_the_raw_accent_and_icon(self, real_team_discord):
        team = FakeTeam(1, "Blue", color="#3355cc", piece_item_id=4151)
        badge = et.team_badge(team, 0)
        assert badge["color"] == "#3355cc"
        assert badge["icon_item_id"] == 4151

    def test_colorless_team_rotates_by_index(self, real_team_discord):
        first = et.team_badge(FakeTeam(1, "A"), 0)
        second = et.team_badge(FakeTeam(2, "B"), 1)
        assert first["orb"] != second["orb"]
        assert first["orb_color"] != second["orb_color"]

    def test_every_orb_has_a_fill(self):
        for icon in etd.TEAM_CHANNEL_ICONS:
            assert icon in etd.ORB_COLORS
