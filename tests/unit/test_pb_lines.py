"""data/submissions/pb_lines.py: kill times parsed from the plugin's chat lines.

The pattern cases are the plugin's former PbHandlerParseTest, moved here with
the parser (raid messages verbatim from RuneLite's ChatCommandsPluginTest, run
through the plugin's sanitising). The replay and expansion cases pin how a
kill's lines combine across ticks.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "submissions", "pb_lines.py")
_spec = importlib.util.spec_from_file_location("_pb_lines_real", _PATH)
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)


def sanitize(message):
    """Mirrors the plugin's SubmissionManager.sanitize."""
    return re.sub(r"<[^>]*>", "", message.replace("<br>", "\n")).replace(" ", " ").strip()


def time_match(message):
    line = pl.select_time_line(sanitize(message))
    assert line is not None, f"expected a usable time line in: {message}"
    m = pl.TIME_WITH_PB_PATTERN.search(line)
    assert m
    return m


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message,key,kind,value", [
    ("Your Zulrah kill count is: 1,234", "Zulrah", "kill", "1,234"),
    ("Your Grotesque Guardians kill count is: 500", "Grotesque Guardians", "kill", "500"),
    ("Your Barrows chest count is: 42", "Barrows", "chest", "42"),
    ("Your Wintertodt success count is: 300", "Wintertodt", "success", "300"),
    ("Your Vet'ion kill count is: 69", "Vet'ion", "kill", "69"),
    ("Your Vardorvis (awakened) kill count is: 3.", "Vardorvis (awakened)", "kill", "3"),
])
def test_boss_count_pattern(message, key, kind, value):
    m = pl.BOSS_COUNT_PATTERN.search(message)
    assert (m.group("key"), m.group("type"), m.group("value")) == (key, kind, value)


def test_boss_count_ignores_unrelated_messages():
    assert not pl.BOSS_COUNT_PATTERN.search("You have a funny feeling...")
    assert not pl.BOSS_COUNT_PATTERN.search("Your heriblore level is now 78.")


@pytest.mark.parametrize("message,kind,key,value", [
    ("Your completed Theatre of Blood count is: 5", "completed", "Theatre of Blood", "5"),
    ("Your subdued Wintertodt count is: 10", "subdued", "Wintertodt", "10"),
    ("Your completed Fractured Archive count is: 3.", "completed", "Fractured Archive", "3"),
])
def test_secondary_boss_pattern(message, kind, key, value):
    m = pl.SECONDARY_BOSS_PATTERN.search(message)
    assert (m.group("type"), m.group("key"), m.group("value")) == (kind, key, value)


def test_completed_counts_are_not_primary_counts():
    assert not pl.BOSS_COUNT_PATTERN.search("Your completed Fractured Archive count is: 3.")


@pytest.mark.parametrize("message,size", [
    ("Team size: Solo", "Solo"), ("Team size: 3 players", "3"), ("Team size: 1 player", "1"),
    # Whole bracket labels (suggestion #153).
    ("Team size: 16-23 players", "16-23"), ("Team size: 11-15 players", "11-15"),
    ("Team size: 24+ players", "24+"), ("Team size: 6+ players", "6+"),
])
def test_team_size_pattern(message, size):
    assert pl.TEAM_SIZE_PATTERN.search(message).group("size") == size


def test_team_size_pattern_ignores_other_text():
    assert not pl.TEAM_SIZE_PATTERN.search("Fight duration: 1:23")


@pytest.mark.parametrize("message,duration,pbtime,indicator", [
    ("Fight duration: 1:23.40 (new personal best)", "1:23.40", None, "(new personal best)"),
    ("Duration: 45:12 Personal best: 40:00", "45:12", "40:00", None),
    ("Overall time: 1:23:45.60 (new personal best)", "1:23:45.60", None, "(new personal best)"),
    ("Fight duration: 2:15.", "2:15", None, None),
])
def test_time_pattern(message, duration, pbtime, indicator):
    m = pl.TIME_WITH_PB_PATTERN.search(message)
    assert (m.group("duration"), m.group("pbtime"), m.group("pbIndicator")) == (duration, pbtime, indicator)


# --------------------------------------------------------------------------- #
# Which line a raid's time is read from
# --------------------------------------------------------------------------- #
TOB_PB = ("Wave 'The Final Challenge' (Normal Mode) complete!<br>Duration: <col=ff0000>2:42.0</col><br>"
          "Theatre of Blood completion time: <col=ff0000>17:00.20</col> (new personal best)")
TOB_NO_PB = ("Wave 'The Final Challenge' (Normal Mode) complete!<br>Duration: <col=ff0000>2:42</col><br>"
             "Theatre of Blood completion time: <col=ff0000>17:00</col>. Personal best: 13:52.80")
TOB_TOTAL = "Theatre of Blood total completion time: <col=ff0000>24:40.20</col>. Personal best: 20:45.00"
TOA_PB = ("Challenge complete: The Wardens. Duration: <col=ef1020>8:30</col><br>"
          "Tombs of Amascut challenge completion time: <col=ef1020>8:31</col> (new personal best)")
TOA_TOTAL = "Tombs of Amascut total completion time: <col=ef1020>0:01</col> (new personal best)"
TFA_PB = "The Fractured Archive: Total completion time: <col=ef1020>2:25.80</col> (new personal best)"
TFA_NO_PB = "The Fractured Archive: Total completion time: <col=ef1020>2:54.60</col>. Personal best: 2:25.80"


def test_tob_takes_the_raid_time_not_the_final_wave():
    m = time_match(TOB_PB)
    assert (m.group("duration"), m.group("pbIndicator"), m.group("pbtime")) == ("17:00.20", "(new personal best)", None)
    assert pl.parse_time(m.group("duration")) == 17 * 60_000 + 200


def test_tob_non_pb_reports_completion_time_and_standing_best():
    m = time_match(TOB_NO_PB)
    assert (m.group("duration"), m.group("pbtime"), m.group("pbIndicator")) == ("17:00", "13:52.80", None)


def test_tfa_total_completion_time_is_the_raid_time():
    m = time_match(TFA_PB)
    assert (m.group("duration"), m.group("pbIndicator")) == ("2:25.80", "(new personal best)")
    m = time_match(TFA_NO_PB)
    assert (m.group("duration"), m.group("pbtime"), m.group("pbIndicator")) == ("2:54.60", "2:25.80", None)


def test_other_wall_clock_totals_are_never_kill_times():
    assert pl.select_time_line(sanitize(TOB_TOTAL)) is None
    assert pl.select_time_line(sanitize(TOA_TOTAL)) is None


def test_toa_takes_the_challenge_time_over_the_wardens():
    m = time_match(TOA_PB)
    assert (m.group("duration"), m.group("pbIndicator")) == ("8:31", "(new personal best)")


def test_cox_completion_is_read_as_is():
    m = time_match("<col=ef20ff>Congratulations - your raid is complete!</col><br>"
                   "Team size: <col=ff0000>4 players</col> Duration:</col> <col=ff0000>37:04.20</col> "
                   "Personal best: </col><col=ff0000>32:26.40</col>")
    assert (m.group("duration"), m.group("pbtime")) == ("37:04.20", "32:26.40")


def test_single_line_kill_times():
    assert time_match("Fight duration: <col=ff0000>1:23.40</col> (new personal best)").group("duration") == "1:23.40"
    m = time_match("Fight duration: <col=ff0000>2:30</col>. Personal best: 2:15.60")
    assert (m.group("duration"), m.group("pbtime")) == ("2:30", "2:15.60")
    assert time_match("Challenge duration: <col=ff0000>10:30</col>. Personal best: 9:45").group("duration") == "10:30"


def test_tob_room_splits_never_become_the_raid_time():
    non_pb = ("Wave 'The Final Challenge' (Normal Mode) complete!<br>Duration: <col=ff0000>5:34.20</col><br>"
              "Theatre of Blood completion time: <col=ff0000>18:54.60</col><br>"
              "Theatre of Blood total completion time: <col=ff0000>21:42.60</col>. Personal best: 20:45.00")
    m = time_match(non_pb)
    assert m.group("duration") == "18:54.60" and m.group("pbIndicator") is None
    room_pb = ("Wave 'The Final Challenge' (Normal Mode) complete!<br>"
               "Duration: <col=ff0000>4:53.80</col> (new personal best)<br>"
               "Theatre of Blood completion time: <col=ff0000>19:14.40</col>")
    assert time_match(room_pb).group("duration") == "19:14.40"


def test_without_pb_info_the_first_timed_line_wins():
    m = time_match("Wave 'The Final Challenge' complete!<br>Duration: <col=ff0000>2:42.0</col><br>"
                   "Something else: <col=ff0000>9:99</col>")
    assert m.group("duration") == "2:42.0"


def test_messages_without_a_time_yield_nothing():
    assert pl.select_time_line("Your completed Theatre of Blood count is: 73.") is None
    assert pl.select_time_line("") is None
    assert pl.select_time_line(None) is None


# --------------------------------------------------------------------------- #
# Team size, delve levels, time formatting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("size,label", [(5, "5"), (4, "4"), (1, "Solo"), (2, "2"), (0, "0")])
def test_raid_team_size(size, label):
    assert pl.raid_team_size(size) == label


def test_delve_levels():
    assert pl.delve_boss("Delve level: 3 duration: 1:23. Personal best: 1:05") == "Doom of Mokhaiotl (Level:3)"
    assert pl.delve_level("Delve level: 3 duration: 1:23. Personal best: 1:05") == 3
    assert pl.delve_boss("Delve level: 8 duration: 1:57. Personal best: 1:44") == "Doom of Mokhaiotl (Level:8)"
    # Past 8 the levels share one board but report the real level.
    assert pl.delve_boss("Delve level: 8+ (9) duration: 1:27 (new personal best)") == "Doom of Mokhaiotl (Level:8+)"
    assert pl.delve_level("Delve level: 8+ (9) duration: 1:27 (new personal best)") == 9
    assert pl.delve_level("Delve level: 8+ (14) duration: 2:10. Personal best: 1:27") == 14
    # The 1 - 8 total is the boss itself.
    assert pl.delve_boss("Delve level 1 - 8 duration: 12:05. Personal best: 11:56") == "Doom of Mokhaiotl"
    assert pl.delve_level("Delve level 1 - 8 duration: 12:05. Personal best: 11:56") is None
    assert pl.select_time_line("Delve level: 8+ (9)") is None
    assert pl.delve_level("Fight duration: 1:23. Personal best: 1:05") is None
    assert pl.delve_level(None) is None


@pytest.mark.parametrize("ms,precise,text", [
    (83_400, True, "01:23.40"), (83_400, False, "01:23"), (5_025_600, True, "01:23:45.60"), (0, False, "00:00"),
])
def test_format_time(ms, precise, text):
    assert pl.format_time(ms, precise) == text


# --------------------------------------------------------------------------- #
# Replay and expansion
# --------------------------------------------------------------------------- #
def line(message, tick=100, party=(0, 0, 0, 1)):
    return {"m": message, "t": tick, "p": list(party)}


def submission(*lines, **extra):
    data = {"player_name": "P", "acc_hash": "42", "guid": "g", "precise_timing": "false", "is_pb": "true",
            "source": "x", "type": "npc_kill"}
    data.update(extra)
    for i, entry in enumerate(lines, start=1):
        data[f"chat_line_{i}"] = json.dumps(entry)
    return data


def no_dupes(boss, count):
    return False


def test_a_boss_kill_becomes_the_fields_the_plugin_sent():
    out = pl.expand(submission(line("Your Vorkath kill count is: 1,234."),
                               line("Fight duration: 1:23.40 (new personal best)")), no_dupes)
    assert len(out) == 1
    kill = out[0]
    assert {k: kill[k] for k in ("boss_name", "kill_time", "best_time", "is_pb", "team_size", "killcount")} == {
        "boss_name": "Vorkath", "kill_time": "01:23", "best_time": "00:00", "is_pb": "true",
        "team_size": "Solo", "killcount": "1234"}
    # The client's own hints never reach the processor; the base fields do.
    assert "source" not in kill and "chat_line_1" not in kill
    assert kill["player_name"] == "P" and kill["guid"] == "g"


@pytest.mark.parametrize("game_name,boss", [
    ("Vardorvis (awakened)", "Vardorvis (Awakened)"),
    ("Duke Sucellus (awakened)", "Duke Sucellus (Awakened)"),
    ("Leviathan (awakened)", "Leviathan (Awakened)"),
    ("Whisperer (awakened)", "Whisperer (Awakened)"),
])
def test_awakened_dt2_kills_are_recorded_under_the_awakened_boss(game_name, boss):
    # Suggestion #199: the parenthesised name used to fail the count pattern,
    # so every awakened kill was dropped.
    out = pl.expand(submission(line(f"Your {game_name} kill count is: 7."),
                               line("Fight duration: 2:05.40. Personal best: 1:58.20")), no_dupes)
    assert [(k["boss_name"], k["killcount"], k["best_time"]) for k in out] == [(boss, "7", "01:58")]


def test_normal_dt2_kills_stay_on_the_normal_boss():
    out = pl.expand(submission(line("Your Vardorvis kill count is: 120."),
                               line("Fight duration: 1:05.40. Personal best: 0:58.20")), no_dupes)
    assert [k["boss_name"] for k in out] == ["Vardorvis"]


def test_raid_team_size_comes_from_the_party_the_client_saw():
    out = pl.expand(submission(line(sanitize(TOA_PB), party=(0, 3, 0, 1)),
                               line("Your completed Tombs of Amascut count is: 12.")), no_dupes)
    assert out[0]["boss_name"] == "Tombs of Amascut" and out[0]["team_size"] == "3"


def test_lines_on_later_ticks_still_complete_the_kill():
    out = pl.expand(submission(line("Your Zulrah kill count is: 5.", 100),
                               line("Fight duration: 2:15. Personal best: 2:00", 102)), no_dupes)
    assert [(k["boss_name"], k["killcount"], k["best_time"]) for k in out] == [("Zulrah", "5", "02:00")]


def test_a_partial_older_than_ten_seconds_is_dropped():
    out = pl.expand(submission(line("Your Zulrah kill count is: 5.", 100),
                               line("Fight duration: 2:15.", 120)), no_dupes)
    assert out == []


def test_each_delve_level_is_its_own_kill():
    out = pl.expand(submission(line("Delve level: 8 duration: 1:57. Personal best: 1:44"),
                               line("Delve level 1 - 8 duration: 12:05. Personal best: 11:56")), no_dupes)
    assert [(k["boss_name"], k.get("delve_level"), k["killcount"]) for k in out] == [
        ("Doom of Mokhaiotl (Level:8)", "8", "N/A"), ("Doom of Mokhaiotl", None, "N/A")]
    # Two kills in one submission get their own ids.
    assert [k["guid"] for k in out] == ["g", "g-1"]


def test_ignored_lines_and_count_only_kills_send_nothing():
    assert pl.expand(submission(line("Preparation time: 1:00"),
                                line("Your Vorkath kill count is: 3.")), no_dupes) == []
    assert pl.expand(submission(line("Your party failed to beat the overall target time of 40:00.")), no_dupes) == []


def test_the_same_kill_twice_is_sent_once():
    last = {}

    def seen(boss, count):
        key = f"{boss}-{count}"
        dup = last.get("k") == key
        last["k"] = key
        return dup

    kill = (line("Your Vorkath kill count is: 3."), line("Fight duration: 1:00."))
    assert len(pl.expand(submission(*kill), seen)) == 1
    assert pl.expand(submission(*kill), seen) == []


def test_precise_timing_keeps_the_hundredths():
    out = pl.expand(submission(line("Your Vorkath kill count is: 3."), line("Fight duration: 1:00.40"),
                               precise_timing="true"), no_dupes)
    assert (out[0]["kill_time"], out[0]["precise_timing"]) == ("01:00.40", "true")


def test_garbage_lines_are_skipped():
    data = submission(line("Your Vorkath kill count is: 3."), line("Fight duration: 1:00."))
    data["chat_line_2"] = "{not json"
    data["chat_line_3"] = json.dumps(line("Fight duration: 1:00."))
    assert [k["boss_name"] for k in pl.expand(data, no_dupes)] == ["Vorkath"]
    assert pl.has_lines(data) and not pl.has_lines({"boss_name": "Vorkath"})
