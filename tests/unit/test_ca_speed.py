"""Combat achievement speed targets parsed from the game-cache catalogue (suggestion #239)."""
import pytest

from utils import ca_speed


def test_every_elite_or_harder_speed_task_parses_or_is_skipped_on_purpose():
    # A rebuilt catalogue (scripts/ca_tasks.json) with new wording must not
    # silently drop a task: it either parses or names a deliberate reason.
    deliberate = {reason for _pattern, reason in ca_speed._NOT_ONE_KILL}
    for task in ca_speed._tasks():
        if str(task.get("tier", "")).lower() not in ca_speed.TIERS:
            continue
        parsed, reason = ca_speed.parse_task(task)
        assert parsed or reason in deliberate, f"{task['name']}: {reason}"


@pytest.mark.parametrize("text,ms", [
    ("54 seconds.", 54_000),
    ("1 minute 45 seconds.", 105_000),
    ("1 minute and 30 seconds without a slayer task.", 90_000),
    ("1:40 minutes.", 100_000),
    ("21 minutes.", 1_260_000),
    ("18 mins at any group size.", 1_080_000),
    ("10:00.", 600_000),
])
def test_parse_duration(text, ms):
    assert ca_speed.parse_duration(text) == ms


def _task(description, tier="Grandmaster", monster="Theatre of Blood"):
    return {"name": "t", "tier": tier, "type": "Speed", "monster": monster, "description": description}


def test_team_sizes_and_inclusive_wording():
    (board, team, target), _ = ca_speed.parse_task(
        _task("Complete the Theatre of Blood (Trio) in less than 17 minutes and 30 seconds."))
    assert (board, team, target.time_ms, target.inclusive) == ("Theatre of Blood", "3", 1_050_000, False)
    (_, team, target), _ = ca_speed.parse_task(
        _task("Complete the Tombs of Amascut (expert) within 18 mins in a group of 8.",
              monster="Tombs of Amascut: Expert Mode"))
    assert (team, target.inclusive) == ("8", True)
    (_, team, _), _ = ca_speed.parse_task(
        _task("Complete a Chambers of Xeric (5-scale) in less than 15 minutes.", monster="Chambers of Xeric"))
    assert team == "5"


def test_tasks_that_are_not_one_timed_kill_are_skipped():
    assert ca_speed.parse_task(_task("Kill Araxxor 6 times in 10:00.", monster="Araxxor"))[0] is None
    assert ca_speed.parse_task(_task("Defeat Yama in an average time of under 2:34 over your last four kills.",
                                     monster="Yama"))[0] is None
    assert ca_speed.parse_task(_task("Kill Vorkath in less than 1 minute.", tier="Hard"))[0] is None


def test_speed_tier_on_real_boards():
    assert ca_speed.speed_tier("Vorkath", "Solo", 53_400) == "grandmaster"
    assert ca_speed.speed_tier("Vorkath", "Solo", 54_000) == "master"  # "less than 54 seconds"
    assert ca_speed.speed_tier("Vorkath", "Solo", 75_000) is None
    # "24:00 or less" includes the target itself.
    assert ca_speed.speed_tier("Fortis Colosseum", "Solo", 1_440_000) == "grandmaster"
    # Team-specific targets only apply to their own board.
    assert ca_speed.speed_tier("Theatre of Blood", "3", 1_049_400) == "grandmaster"
    assert ca_speed.speed_tier("Theatre of Blood", "5", 1_049_400) is None
    # Board aliases: the Crystalline Hunllef task is The Gauntlet's board.
    assert ca_speed.speed_tier("The Gauntlet", "Solo", 224_400) == "grandmaster"
    assert ca_speed.speed_tier("Chambers of Xeric Challenge Mode", "Solo", 2_300_000) == "grandmaster"


def test_meets_tier_counts_harder_tiers_and_needs_a_task():
    assert ca_speed.meets_tier("Vorkath", "Solo", 53_400, "master")
    assert not ca_speed.meets_tier("Vorkath", "Solo", 60_000, "grandmaster")
    # Hespori has no Grandmaster speed task.
    assert not ca_speed.meets_tier("Hespori", "Solo", 1_000, "grandmaster")
    # The Inferno has only a Grandmaster task, which also meets Master.
    assert ca_speed.meets_tier("TzKal-Zuk", "Solo", 3_800_000, "master")
    assert not ca_speed.meets_tier("Zulrah", "Solo", 1, "bogus")
