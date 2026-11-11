"""Profile overview: diary task merge, XP history arithmetic, feed shaping."""
import json
from datetime import date, datetime
from types import SimpleNamespace

from utils.diary_tasks import diary_reference, merge_diaries, tier_totals
from utils.xp_history import (
    SKILLS,
    gains_since,
    level_for_xp,
    period_starts,
    skill_summary,
    xp_for_level,
)

REFERENCE = [{
    "area_id": 0, "name": "Karamja", "wikisync": "Karamja",
    "tiers": [
        {"tier": 0, "name": "Easy", "tasks": [{"description": "a"}, {"description": "b"}, {"description": "c"}]},
        {"tier": 1, "name": "Medium", "tasks": [{"description": "d"}, {"description": "e"}]},
    ],
}]


def _states(area, tier):
    return [t["completed"] for t in area["tiers"][tier]["tasks"]]


class TestDiaryReference:
    def test_committed_file_matches_the_games_task_counts(self):
        # scripts/sync_diary_tasks.EXPECTED_COUNTS, from the game's own script.
        from scripts.sync_diary_tasks import EXPECTED_COUNTS

        totals = tier_totals()
        assert len(diary_reference()) == 12
        for area_id, counts in EXPECTED_COUNTS.items():
            for tier, expected in enumerate(counts):
                assert totals[(area_id, tier)] == expected

    def test_every_task_has_text(self):
        for area in diary_reference():
            for tier in area["tiers"]:
                assert all(t["description"] for t in tier["tasks"])


class TestMergeDiaries:
    def test_full_and_empty_tiers_need_no_task_list(self):
        [area] = merge_diaries({(0, 0): 3, (0, 1): 0}, None, REFERENCE)
        assert _states(area, 0) == [True, True, True]
        assert _states(area, 1) == [False, False]
        assert area["tiers"][0]["complete"] is True
        assert area["tiers"][0]["task_source"] == "sync"
        assert area["completed"] == 3 and area["total"] == 5

    def test_partial_tier_without_wikisync_is_unknown_not_guessed(self):
        [area] = merge_diaries({(0, 0): 2}, None, REFERENCE)
        assert _states(area, 0) == [None, None, None]
        assert area["tiers"][0]["tasks_known"] is False
        assert area["tiers"][0]["completed"] == 2

    def test_wikisync_fills_a_partial_tier_when_counts_agree(self):
        ws = {"Karamja": {"Easy": {"complete": False, "tasks": [True, False, True]}}}
        [area] = merge_diaries({(0, 0): 2}, ws, REFERENCE)
        assert _states(area, 0) == [True, False, True]
        assert area["tiers"][0]["task_source"] == "wikisync"

    def test_stale_wikisync_is_ignored(self):
        # WikiSync last saw one task done; the plugin has since synced two.
        ws = {"Karamja": {"Easy": {"complete": False, "tasks": [True, False, False]}}}
        [area] = merge_diaries({(0, 0): 2}, ws, REFERENCE)
        assert _states(area, 0) == [None, None, None]

    def test_wikisync_of_the_wrong_shape_is_ignored(self):
        for ws in (
            {"Karamja": {"Easy": {"tasks": [True, False]}}},          # wrong length
            {"Karamja": {"Easy": {"tasks": [1, 0, 1]}}},              # not booleans
            {"Karamja": "nope"}, {"Karamja": {"Easy": None}}, "nope",
        ):
            [area] = merge_diaries({(0, 0): 2}, ws, REFERENCE)
            assert _states(area, 0) == [None, None, None]

    def test_never_synced_tier_has_no_count(self):
        [area] = merge_diaries({}, None, REFERENCE)
        assert area["tiers"][0]["completed"] is None
        assert area["tiers"][0]["complete"] is False

    def test_count_above_the_tier_size_is_clamped(self):
        [area] = merge_diaries({(0, 1): 9}, None, REFERENCE)
        assert area["tiers"][1]["completed"] == 2 and area["tiers"][1]["complete"]


class TestLevels:
    def test_known_thresholds(self):
        assert xp_for_level(2) == 83
        assert xp_for_level(99) == 13_034_431
        assert level_for_xp(0) == 1
        assert level_for_xp(82) == 1
        assert level_for_xp(83) == 2
        assert level_for_xp(13_034_430) == 98
        assert level_for_xp(200_000_000) == 99
        assert level_for_xp(None) == 1

    def test_summary_totals_and_hitpoints_floor(self):
        summary = skill_summary({s: 0 for s in SKILLS})
        assert summary["total_level"] == len(SKILLS) - 1 + 10
        maxed = skill_summary({s: 13_034_431 for s in SKILLS})
        assert maxed["total_level"] == 99 * len(SKILLS)
        assert all(s["progress"] == 100.0 for s in maxed["skills"])


class TestGainsSince:
    now = datetime(2026, 11, 10, 12, 0)

    def _gains(self, current, rows):
        """Pick baselines out of ``rows`` the way the store does."""
        rows = sorted(rows)
        baselines = {}
        for period, start in period_starts(self.now).items():
            older = [r for r in rows if r[0] <= start]
            baselines[period] = older[-1] if older else None
        return gains_since(current, baselines, rows[0] if rows else None, self.now)

    def test_no_history_is_none(self):
        assert self._gains({"attack": 500}, []) == {"day": None, "week": None, "month": None}

    def test_baseline_is_the_latest_row_at_or_before_the_period_start(self):
        rows = [
            (datetime(2026, 10, 1, 9), {"attack": 100, "magic": 50}),
            (datetime(2026, 11, 2, 9), {"attack": 300, "magic": 50}),
            (datetime(2026, 11, 9, 11, 59), {"attack": 450, "magic": 60}),
            (datetime(2026, 11, 9, 12, 1), {"attack": 480, "magic": 60}),
        ]
        out = self._gains({"attack": 500, "magic": 60}, rows)
        assert out["day"]["skills"] == {"attack": 50}
        assert out["week"]["skills"] == {"attack": 200, "magic": 10}
        assert out["month"]["skills"] == {"attack": 400, "magic": 10}
        assert out["month"]["total"] == 410
        assert out["week"]["partial"] is False
        assert out["week"]["since"] == int(datetime(2026, 11, 3, 12).timestamp())

    def test_short_history_uses_the_earliest_row_and_says_so(self):
        first = datetime(2026, 11, 8, 20, 30)
        out = self._gains({"attack": 500}, [(first, {"attack": 400})])
        assert out["month"]["partial"] is True
        assert out["month"]["skills"] == {"attack": 100}
        assert out["month"]["since"] == int(first.timestamp())
        assert out["day"]["partial"] is False

    def test_a_drop_in_xp_is_not_a_negative_gain(self):
        out = self._gains({"attack": 100}, [(datetime(2026, 11, 1), {"attack": 400})])
        assert out["week"] == {"total": 0, "skills": {}, "since": out["week"]["since"], "partial": False}

    def test_a_skill_the_baseline_lacks_is_not_counted(self):
        out = self._gains({"attack": 100, "sailing": 900}, [(datetime(2026, 11, 1), {"attack": 40})])
        assert out["week"]["skills"] == {"attack": 60}


class TestFeedShaping:
    def test_queue_rows_fold_to_one_item_per_event(self):
        from web_api.routes.player_overview import notification_feed_items

        when = datetime(2026, 11, 9, 12, 0)
        later = datetime(2026, 11, 9, 12, 5)
        level = json.dumps({"skill_name": "Fletching", "new_level": 85})
        rows = [
            ("level_up", level, later),
            ("level_up", level, when),  # same event, another group's row
            ("level_up", json.dumps({"skill_name": "Fletching", "new_level": 12}), when),
            ("kc_milestone", json.dumps({"npc_name": "Zulrah", "npc_id": 2042, "milestone": 500}), when),
            ("total_level_milestone", json.dumps({"total_level": 2000}), when),
            ("level_up", "{broken", when),
            ("something_else", "{}", when),
        ]
        items = {i["type"]: i for i in notification_feed_items(rows)}
        assert set(items) == {"level", "kc", "total_level"}
        assert items["level"]["title"] == "85 Fletching"
        assert items["level"]["ts"] == int(when.timestamp())
        assert items["kc"]["title"] == "500 Zulrah kills"
        assert items["kc"]["icon_url"].endswith("/npcdb/2042.png")
        assert items["total_level"]["title"] == "2,000 total level"

    def test_wom_gains_keep_only_what_was_gained(self):
        from web_api.routes.player_overview import shape_wom_achievements, shape_wom_gains

        body = {"startsAt": "a", "endsAt": "b", "data": {
            "skills": {
                "overall": {"experience": {"gained": 300}},
                "attack": {"experience": {"gained": 300}},
                "magic": {"experience": {"gained": 0}},
            },
            "bosses": {"zulrah": {"kills": {"gained": 4}}, "vorkath": {"kills": {"gained": 0}}},
            "computed": {"ehp": {"value": {"gained": 0.5}}},
        }}
        out = shape_wom_gains(body)
        assert out["skills"] == {"attack": 300} and out["total_xp"] == 300
        assert out["bosses"] == [{"metric": "zulrah", "kills": 4}]
        assert out["ehp"] == 0.5 and out["ehb"] == 0.0
        assert shape_wom_gains({"nope": 1}) is None

        achievements = shape_wom_achievements([
            {"name": "old", "metric": "x", "createdAt": "1970-01-01T00:00:00.000Z"},
            {"name": "b", "metric": "x", "createdAt": "2026-08-01T00:00:00.000Z"},
            {"name": "a", "metric": "x", "createdAt": "2026-09-01T00:00:00.000Z"},
        ])
        assert [a["name"] for a in achievements] == ["a", "b"]
