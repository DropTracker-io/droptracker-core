"""Unit tests for the timed-boost engine (``modify_for_event``) in
``data/submissions/point_awards.py``.

Covers window/reason/target matching, the legacy scalar ``target_id`` path,
the multi-target ``target_ids`` JSON path, and all four operations including
``add_per_member`` (+value per in-group participant present).
"""

import json
from types import SimpleNamespace

from data.submissions.point_awards import (
    _parse_boost_target_ids,
    modify_for_event,
)

NOW = 1_700_000_000
GROUP_ID = 42
PLAYER_ID = 7


def make_boost(
    boost_id=1,
    start=NOW - 3600,
    end=NOW + 3600,
    event_type="any",
    target_type="any",
    target_id=None,
    target_ids=None,
    operation="multiply",
    operation_value=2,
):
    return SimpleNamespace(
        id=boost_id,
        group_id=GROUP_ID,
        start_time_unix=start,
        end_time_unix=end,
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        target_ids=json.dumps(target_ids) if isinstance(target_ids, list) else target_ids,
        operation=operation,
        operation_value=operation_value,
    )


class FakeSession:
    """Mimics session.query(...).filter(...).order_by(...).all() for boosts."""

    def __init__(self, boosts):
        self._boosts = boosts

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        # Engine orders newest-id-first; emulate the DB sort.
        return sorted(self._boosts, key=lambda b: b.id, reverse=True)


async def run(boosts, default_value=10, **kwargs):
    kwargs.setdefault("submission_timestamp", NOW)
    return await modify_for_event(
        "drop",
        GROUP_ID,
        PLAYER_ID,
        default_value,
        external_session=FakeSession(boosts),
        **kwargs,
    )


class TestParseTargetIds:
    def test_json_array(self):
        assert _parse_boost_target_ids("[1, 2, 3]") == [1, 2, 3]

    def test_null_and_empty(self):
        assert _parse_boost_target_ids(None) == []
        assert _parse_boost_target_ids("") == []

    def test_malformed_falls_back_to_empty(self):
        assert _parse_boost_target_ids("{not json") == []
        assert _parse_boost_target_ids('"scalar"') == []

    def test_string_numbers_coerced(self):
        assert _parse_boost_target_ids('["4", 5]') == [4, 5]


class TestWindowAndReason:
    async def test_no_boosts_returns_default(self):
        assert await run([]) == 10

    async def test_active_window_applies(self):
        assert await run([make_boost()]) == 20

    async def test_expired_window_skipped(self):
        boost = make_boost(start=NOW - 7200, end=NOW - 3600)
        assert await run([boost]) == 10

    async def test_future_window_skipped(self):
        boost = make_boost(start=NOW + 3600, end=NOW + 7200)
        assert await run([boost]) == 10

    async def test_reason_mismatch_skipped(self):
        assert await run([make_boost(event_type="pb")]) == 10

    async def test_reason_match_applies(self):
        assert await run([make_boost(event_type="drop")]) == 20

    async def test_newest_boost_wins_no_stacking(self):
        older = make_boost(boost_id=1, operation="multiply", operation_value=2)
        newer = make_boost(boost_id=2, operation="add", operation_value=5)
        assert await run([older, newer]) == 15  # add wins, multiply never applies


class TestTargetMatching:
    async def test_legacy_scalar_item_match(self):
        boost = make_boost(target_type="item", target_id=13652)
        assert await run([boost], item_id=13652) == 20

    async def test_legacy_scalar_item_mismatch(self):
        boost = make_boost(target_type="item", target_id=13652)
        assert await run([boost], item_id=999) == 10

    async def test_scalar_zero_matches_any_item(self):
        boost = make_boost(target_type="item", target_id=0)
        assert await run([boost], item_id=999) == 20

    async def test_item_boost_needs_item_id(self):
        boost = make_boost(target_type="item", target_id=13652)
        assert await run([boost], item_id=None) == 10

    async def test_multi_target_item_match(self):
        # The thread's exact use case: one boost, three CoX uniques.
        boost = make_boost(target_type="item", target_ids=[21079, 24670, 20851])
        assert await run([boost], item_id=24670) == 20

    async def test_multi_target_item_mismatch(self):
        boost = make_boost(target_type="item", target_ids=[21079, 24670])
        assert await run([boost], item_id=4151) == 10

    async def test_multi_target_wins_over_stale_scalar(self):
        # When both columns are set, the list is authoritative.
        boost = make_boost(target_type="item", target_id=111, target_ids=[222])
        assert await run([boost], item_id=111) == 10
        assert await run([boost], item_id=222) == 20

    async def test_multi_target_npc_match(self):
        boost = make_boost(target_type="npc", target_ids=[3029, 3030])
        assert await run([boost], npc_id=3030) == 20
        assert await run([boost], npc_id=1) == 10

    async def test_malformed_target_ids_falls_back_to_scalar(self):
        boost = make_boost(target_type="item", target_id=555, target_ids="{broken")
        assert await run([boost], item_id=555) == 20


class TestOperations:
    async def test_multiply(self):
        assert await run([make_boost(operation="multiply", operation_value=3)]) == 30

    async def test_add(self):
        assert await run([make_boost(operation="add", operation_value=7)]) == 17

    async def test_set(self):
        assert await run([make_boost(operation="set", operation_value=100)]) == 100

    async def test_unknown_operation_returns_default(self):
        assert await run([make_boost(operation="divide", operation_value=2)]) == 10


class TestAddPerMember:
    async def test_solo_counts_as_one(self):
        boost = make_boost(operation="add_per_member", operation_value=5)
        assert await run([boost], present_member_count=1) == 15

    async def test_scales_with_members_present(self):
        boost = make_boost(operation="add_per_member", operation_value=5)
        assert await run([boost], present_member_count=4) == 30  # 10 + 5*4

    async def test_missing_count_treated_as_solo(self):
        boost = make_boost(operation="add_per_member", operation_value=5)
        assert await run([boost]) == 15

    async def test_zero_or_garbage_count_clamped_to_one(self):
        boost = make_boost(operation="add_per_member", operation_value=5)
        assert await run([boost], present_member_count=0) == 15
        assert await run([boost], present_member_count="junk") == 15

    async def test_combines_with_item_targeting(self):
        boost = make_boost(
            operation="add_per_member",
            operation_value=10,
            target_type="item",
            target_ids=[100, 200],
        )
        assert await run([boost], item_id=200, present_member_count=3) == 40
        assert await run([boost], item_id=300, present_member_count=3) == 10
