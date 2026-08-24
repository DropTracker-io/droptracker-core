"""Combat Achievement tier + progress rendering.

Field reports: a player sitting just short of Grandmaster was told
"Current tier: None (2,412 pts) / Progress to **Easy**: 0% (-2,412 pts)".

Two independent defects produced that. The wiki threshold lookup ran inline,
uncached, twice per notification, and when it came back empty the handler used
``next_tier_points = 38`` with ``current_tier = None`` — so points_left went
hugely negative and the next tier read "Easy". Separately, indexing a
*descending* tier list with ``index(tier) - 1`` wrapped past the end, so a
finished Grandmaster was also told their next tier was Easy.
"""

import pytest

from services.ca_tiers import (
    CA_TIER_ORDER,
    FALLBACK_TIER_POINTS,
    build_threshold_table,
    ca_progress,
    get_tier_thresholds,
    parse_threshold,
    reset_cache,
)

# The live wiki values as of 2026-08-23.
LIVE = {
    "Easy": 41,
    "Medium": 161,
    "Hard": 419,
    "Elite": 1075,
    "Master": 1940,
    "Grandmaster": 2672,
}


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def test_near_grandmaster_player_is_pointed_at_grandmaster():
    """The reported bug: 2,412 points is deep in Master, not short of Easy."""
    result = ca_progress(2412, LIVE)

    assert result["current_tier"] == "Master"
    assert result["next_tier"] == "Grandmaster"
    assert result["next_tier_points"] == 2672
    assert result["points_left"] == 260
    assert float(result["progress"]) == pytest.approx(64.48, abs=0.01)


def test_points_left_is_never_negative_and_tier_never_understated():
    """Whatever the thresholds, a high total can't come out as "below Easy"."""
    for total in (500, 1200, 2000, 2412, 3000):
        result = ca_progress(total, LIVE)
        assert result["points_left"] >= 0
        assert result["current_tier"] != "None"


def test_grandmaster_complete_does_not_wrap_around_to_easy():
    result = ca_progress(2700, LIVE)

    assert result["current_tier"] == "Grandmaster"
    assert result["next_tier"] == "Grandmaster"
    assert result["points_left"] == 0
    assert result["progress"] == "100"


def test_exact_threshold_counts_as_having_reached_the_tier():
    assert ca_progress(2672, LIVE)["current_tier"] == "Grandmaster"
    assert ca_progress(1940, LIVE)["current_tier"] == "Master"
    assert ca_progress(1939, LIVE)["current_tier"] == "Elite"


def test_below_easy_has_no_tier_but_still_counts_up_to_easy():
    result = ca_progress(20, LIVE)

    assert result["current_tier"] == "None"
    assert result["next_tier"] == "Easy"
    assert result["points_left"] == 21
    assert float(result["progress"]) == pytest.approx(48.78, abs=0.01)


def test_unknown_total_says_unknown_instead_of_inventing_zero():
    """webhook.py sends total_points=0 for web/Discord manual CA submissions —
    the player has a tier, we just can't read their varbit."""
    result = ca_progress(0, LIVE)

    assert result["known"] is False
    assert result["current_tier"] == "Unknown"
    assert result["next_tier"] == "next tier"
    assert result["points_left"] == "?"
    assert result["total_points"] == "?"
    assert result["progress"] == "?"


@pytest.mark.parametrize("bad", [None, "", "  ", "not a number"])
def test_unparseable_total_is_treated_as_unknown(bad):
    assert ca_progress(bad, LIVE)["known"] is False


def test_progress_falls_back_to_pinned_thresholds_when_none_supplied():
    result = ca_progress(2412, None)

    assert result["current_tier"] == "Master"
    assert result["next_tier"] == "Grandmaster"
    assert result["points_left"] >= 0


# ── threshold table parsing ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [("2672", 2672), ("2,672", 2672), (" 2672\n", 2672), (2672, 2672),
     (None, None), ("", None), ("0", None), ("-5", None), ("banana", None)],
)
def test_parse_threshold(raw, expected):
    assert parse_threshold(raw) == expected


def test_build_threshold_table_accepts_a_full_ascending_answer():
    assert build_threshold_table({k: str(v) for k, v in LIVE.items()}) == LIVE


def test_build_threshold_table_rejects_a_partial_answer():
    """Three good tiers and three blanks is what mis-ranked the player; a
    partial table must fall back rather than be used."""
    partial = dict(LIVE)
    partial["Master"] = None
    partial["Grandmaster"] = ""
    assert build_threshold_table(partial) is None


def test_build_threshold_table_rejects_out_of_order_values():
    scrambled = dict(LIVE, Master=99)
    assert build_threshold_table(scrambled) is None


# ── cached fetch ──────────────────────────────────────────────────────────────

class _Semantic:
    def __init__(self, values):
        self.values = values
        self.calls = 0

    async def get_global_value(self, variable):
        self.calls += 1
        return self.values.get(variable)


def _wiki_values(table):
    from services.ca_tiers import WIKI_GLOBALS
    return {WIKI_GLOBALS[tier]: str(points) for tier, points in table.items()}


async def test_thresholds_are_fetched_once_and_cached():
    semantic = _Semantic(_wiki_values(LIVE))

    assert await get_tier_thresholds(semantic) == LIVE
    assert await get_tier_thresholds(semantic) == LIVE
    assert semantic.calls == len(CA_TIER_ORDER)


async def test_failed_lookup_falls_back_instead_of_producing_nonsense():
    """The exact failure that shipped the bad embed: every lookup returns None."""
    semantic = _Semantic({})

    thresholds = await get_tier_thresholds(semantic)
    assert thresholds == FALLBACK_TIER_POINTS

    result = ca_progress(2412, thresholds)
    assert result["current_tier"] == "Master"
    assert result["next_tier"] == "Grandmaster"
    assert result["points_left"] > 0


async def test_a_raising_lookup_is_swallowed():
    class _Boom:
        async def get_global_value(self, variable):
            raise RuntimeError("wiki down")

    assert await get_tier_thresholds(_Boom()) == FALLBACK_TIER_POINTS


async def test_a_later_failure_keeps_serving_the_last_good_table():
    good = _Semantic(_wiki_values(LIVE))
    assert await get_tier_thresholds(good) == LIVE

    import services.ca_tiers as ca_tiers
    ca_tiers._cached_at = 0.0  # expire the TTL without clearing the cache
    ca_tiers._last_attempt = None

    assert await get_tier_thresholds(_Semantic({})) == LIVE
