"""Which personal bests a group posts (suggestion #239)."""
from utils.pb_notify_filter import PbNotifyRules, parse_bosses, withheld_reason


def _rank(value, calls=None):
    def lookup():
        if calls is not None:
            calls.append(1)
        return value
    return lookup


def _reason(config, *, npc="Vorkath", team="Solo", time_ms=53_400, old=60_000, clan=1, glob=1, calls=None):
    return withheld_reason(
        PbNotifyRules.from_config(config), npc_name=npc, team_size=team, time_ms=time_ms,
        old_time_ms=old, clan_rank=_rank(clan, calls), global_rank=_rank(glob, calls),
    )


def test_no_settings_posts_everything_without_ranking():
    calls = []
    assert not PbNotifyRules.from_config({}).active
    assert _reason({}, calls=calls) is None
    assert calls == []


def test_clan_and_global_top():
    assert _reason({"pb_notify_top_ranks": "3"}, clan=3) is None
    assert _reason({"pb_notify_top_ranks": "3"}, clan=4) == "met none of the group's conditions"
    assert _reason({"pb_notify_global_top_ranks": "50"}, glob=51) is not None
    assert _reason({"pb_notify_global_top_ranks": "50"}, glob=50) is None


def test_an_unknown_rank_counts_as_met():
    assert _reason({"pb_notify_top_ranks": "3"}, clan=None) is None


def test_speed_tier():
    assert _reason({"pb_notify_speed_tier": "grandmaster"}, time_ms=53_400) is None
    assert _reason({"pb_notify_speed_tier": "grandmaster"}, time_ms=60_000) is not None
    assert _reason({"pb_notify_speed_tier": "master"}, time_ms=60_000) is None
    assert _reason({"pb_notify_speed_tier": "none"}, time_ms=999_000) is None


def test_any_or_all_conditions():
    config = {"pb_notify_top_ranks": "3", "pb_notify_speed_tier": "grandmaster"}
    # Slow but top of the clan: any = posted, all = held back.
    assert _reason(config, time_ms=70_000, clan=1) is None
    assert _reason({**config, "pb_notify_match": "all"}, time_ms=70_000, clan=1) == "not a Grandmaster time"
    assert _reason({**config, "pb_notify_match": "all"}, time_ms=53_400, clan=1) is None


def test_ranks_are_only_looked_up_when_needed():
    calls = []
    # The speed condition already passed: "any" never ranks.
    _reason({"pb_notify_speed_tier": "grandmaster", "pb_notify_global_top_ranks": "10"}, calls=calls)
    assert calls == []


def test_boss_and_team_limits():
    bosses = {"pb_notify_bosses": "Zulrah, The Gauntlet"}
    assert _reason(bosses, npc="Vorkath") == "not one of the group's bosses"
    assert _reason(bosses, npc="Crystalline Hunllef") is None  # same board as The Gauntlet
    assert _reason({"pb_notify_team": "group"}, team="Solo") == "not a group time"
    assert _reason({"pb_notify_team": "group"}, team="4") is None
    assert _reason({"pb_notify_team": "solo"}, team="1") is None


def test_limits_apply_whatever_the_conditions_say():
    config = {"pb_notify_bosses": "Zulrah", "pb_notify_top_ranks": "3"}
    assert _reason(config, npc="Vorkath", clan=1) == "not one of the group's bosses"


def test_improvement_and_first_times():
    assert _reason({"pb_notify_min_improvement": "5"}, time_ms=57_000, old=60_000) is not None
    assert _reason({"pb_notify_min_improvement": "5"}, time_ms=55_000, old=60_000) is None
    # A first time has nothing to beat.
    assert _reason({"pb_notify_min_improvement": "5"}, old=None) is None
    assert _reason({"pb_notify_skip_first": "1"}, old=None) == "the member's first time at this boss"
    assert _reason({"pb_notify_skip_first": "1"}, old=60_000) is None


def test_parse_bosses_accepts_the_legacy_bracket_format():
    assert parse_bosses('["Zulrah", "Vorkath"]') == parse_bosses("Zulrah,Vorkath")
    assert parse_bosses("") == frozenset()
