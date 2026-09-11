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

import sys

import pytest

from services.ca_tiers import (
    CA_TIER_ORDER,
    FALLBACK_TIER_POINTS,
    build_threshold_table,
    ca_progress,
    ca_tier_summary,
    current_tier_thresholds,
    get_tier_thresholds,
    parse_threshold,
    reset_cache,
    tier_thresholds_nowait,
)


def _real_ca_tiers():
    """The module itself, for poking its cache.

    Not ``import services.ca_tiers as ca_tiers``: conftest stubs the
    ``services`` package with a MagicMock, and that form resolves the name as
    an attribute of the package, handing back a mock. Writes to the mock
    succeed silently and change nothing, so a test built on them passes (or
    hangs) without exercising the code. ``from services.ca_tiers import x``
    is unaffected — it reads sys.modules, where conftest put the real module.
    """
    return sys.modules["services.ca_tiers"]


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

    ca_tiers = _real_ca_tiers()
    ca_tiers._cached_at = 0.0  # expire the TTL without clearing the cache
    ca_tiers._last_attempt = None

    assert await get_tier_thresholds(_Semantic({})) == LIVE


def test_the_pinned_fallback_is_a_table_the_live_lookup_would_accept():
    # Complete and strictly ascending — the same bar a wiki answer must clear.
    assert build_threshold_table(FALLBACK_TIER_POINTS) == FALLBACK_TIER_POINTS


# ── typed summary for the Data API ────────────────────────────────────────────

# The wiki as of 2026-09-10, after the 2026-08-26 batch.
CURRENT = {
    "Easy": 41,
    "Medium": 169,
    "Hard": 436,
    "Elite": 1100,
    "Master": 1965,
    "Grandmaster": 2697,
}


def test_summary_mid_tier():
    assert ca_tier_summary(656, CURRENT) == {
        "tier": "Hard",
        "next_tier": "Elite",
        "next_tier_points": 1100,
        "points_to_next": 444,
        "progress": pytest.approx(33.13, abs=0.005),
    }


def test_summary_values_are_numbers_not_display_strings():
    summary = ca_tier_summary(2412, CURRENT)
    assert isinstance(summary["next_tier_points"], int)
    assert isinstance(summary["points_to_next"], int)
    assert isinstance(summary["progress"], float)


def test_summary_zero_is_an_account_with_nothing_done_not_unknown():
    # Unlike the notification, where 0 only ever means "a manual submission
    # could not read the varbit", a stored 0 is a real total.
    assert ca_tier_summary(0, CURRENT) == {
        "tier": None,
        "next_tier": "Easy",
        "next_tier_points": 41,
        "points_to_next": 41,
        "progress": 0.0,
    }


def test_summary_exact_threshold_reaches_the_tier():
    summary = ca_tier_summary(169, CURRENT)
    assert summary["tier"] == "Medium"
    assert summary["next_tier"] == "Hard"
    assert summary["progress"] == 0.0


def test_summary_grandmaster_has_nowhere_left_to_go():
    for total in (2697, 2800):
        assert ca_tier_summary(total, CURRENT) == {
            "tier": "Grandmaster",
            "next_tier": None,
            "next_tier_points": None,
            "points_to_next": 0,
            "progress": 100.0,
        }


@pytest.mark.parametrize("bad", [None, True, -5, "abc", ""])
def test_summary_unknown_total_is_all_null(bad):
    assert set(ca_tier_summary(bad, CURRENT).values()) == {None}


def test_summary_uses_the_fallback_without_a_table():
    assert ca_tier_summary(656, None) == ca_tier_summary(656, FALLBACK_TIER_POINTS)


def test_summary_and_notification_agree_on_the_tier():
    # One set of maths behind both surfaces.
    for total in range(1, 3000, 37):
        progress = ca_progress(total, CURRENT)
        summary = ca_tier_summary(total, CURRENT)
        expected = None if progress["current_tier"] == "None" else progress["current_tier"]
        assert summary["tier"] == expected, total
        if summary["next_tier"] is not None:
            assert summary["next_tier"] == progress["next_tier"], total
            assert summary["points_to_next"] == progress["points_left"], total


# ── thresholds without waiting ────────────────────────────────────────────────

def test_current_thresholds_are_the_fallback_until_a_lookup_answers():
    assert current_tier_thresholds() == FALLBACK_TIER_POINTS


def test_current_thresholds_are_a_copy():
    current_tier_thresholds()["Easy"] = 1
    assert current_tier_thresholds()["Easy"] == FALLBACK_TIER_POINTS["Easy"]


def test_nowait_outside_an_event_loop_only_answers():
    ca_tiers = _real_ca_tiers()

    assert tier_thresholds_nowait() == FALLBACK_TIER_POINTS
    assert ca_tiers._refresh_task is None


async def test_nowait_answers_now_and_refreshes_in_the_background(monkeypatch):
    import asyncio

    ca_tiers = _real_ca_tiers()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_wiki(semantic=None):
        started.set()
        await release.wait()
        return dict(CURRENT)

    monkeypatch.setattr(ca_tiers, "_fetch_from_wiki", slow_wiki)

    # Answers with what it has while the wiki is still being asked.
    assert tier_thresholds_nowait() == FALLBACK_TIER_POINTS
    task = ca_tiers._refresh_task
    assert task is not None
    await started.wait()

    # A second request during the refresh starts no second lookup.
    tier_thresholds_nowait()
    assert ca_tiers._refresh_task is task

    release.set()
    await task
    assert tier_thresholds_nowait() == CURRENT
    # Fresh now: nothing further is scheduled.
    assert ca_tiers._refresh_task is task


async def test_nowait_does_not_refresh_a_fresh_table(monkeypatch):
    ca_tiers = _real_ca_tiers()

    await get_tier_thresholds(_Semantic(_wiki_values(CURRENT)))

    async def must_not_run(semantic=None):
        raise AssertionError("a fresh table must not be re-fetched")

    monkeypatch.setattr(ca_tiers, "_fetch_from_wiki", must_not_run)
    assert tier_thresholds_nowait() == CURRENT
    assert ca_tiers._refresh_task is None
