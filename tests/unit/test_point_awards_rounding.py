"""Regression tests for GP-threshold rounding in ``_check_and_award_points``
(``data/submissions/point_awards.py``).

Contract (user-reported fix, 2026-09-23): a group's drop rule is a threshold,
not a rate to round to. Under "1 point per 1m" a drop worth 800k awards
nothing — the old half-up division paid a full point for anything from 500k
up, which handed out points below the minimum the clan configured. Every GP
path (default rule, stacked and unstacked quantities, and a per-item/NPC
override with a divisor) floors the same way.
"""

import json
from types import SimpleNamespace

import data.submissions.common as common
import data.submissions.point_awards as pa

NOW = 1_700_000_000
GROUP_ID = 42
RECEIVER_ID = 1
ITEM_ID = 13652
NPC_ID = 8061


class FakeSession:
    """Inert session: no boosts, no rows."""

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return []

    def first(self):
        return None


def _patch_pipeline(monkeypatch, *, point_config="1,1000000", mods=None,
                    stacks_award_points=True):
    """Stub every external dependency of _check_and_award_points, leaving the
    award formula real. Returns the list of (player_id, points) paid out."""
    awards = []

    monkeypatch.setattr(common, "check_group_point_system_active",
                        lambda group_id, external_session=None: True)

    async def fake_point_config(group_id, external_session=None):
        return {"drop": point_config}

    async def fake_list_rules(group_id=None, item_id=None, npc_id=None,
                              external_session=None):
        return True, False

    async def fake_mods(group_id, external_session=None):
        return mods

    async def fake_stack_config(group_id, external_session=None):
        return stacks_award_points

    async def fake_split_capability(group_id, player_id, external_session=None):
        return True

    async def fake_group_only(group_id, external_session=None):
        return False

    async def fake_limits(group_id, external_session=None):
        return (0, 0)

    async def fake_sharing(group_id, external_session=None):
        return False, "no_split"

    async def fake_award(reason, group_id, player_id, points,
                         entry_id=None, external_session=None):
        awards.append((int(player_id), int(points)))
        return int(points)

    monkeypatch.setattr(pa, "get_point_config", fake_point_config)
    monkeypatch.setattr(pa, "evaluate_point_list_rules", fake_list_rules)
    monkeypatch.setattr(pa, "get_group_point_mods", fake_mods)
    monkeypatch.setattr(pa, "get_group_point_stack_config", fake_stack_config)
    monkeypatch.setattr(pa, "check_split_capability", fake_split_capability)
    monkeypatch.setattr(pa, "check_points_require_group_only_mode", fake_group_only)
    monkeypatch.setattr(pa, "get_group_submission_point_limits", fake_limits)
    monkeypatch.setattr(pa, "check_group_points_sharing", fake_sharing)
    monkeypatch.setattr(pa, "award_player_points", fake_award)
    monkeypatch.setattr(pa, "_get_player_name_by_id",
                        lambda player_id, session: "Receiver")
    monkeypatch.setattr(pa, "_get_player_id_by_name", lambda name, session: None)
    return awards


async def run(monkeypatch, *, value, quantity=1, **patch_kwargs):
    _patch_pipeline(monkeypatch, **patch_kwargs)
    return await pa._check_and_award_points(
        "drop",
        GROUP_ID,
        RECEIVER_ID,
        value,
        players_included=json.dumps([]),
        item_id=ITEM_ID,
        npc_id=NPC_ID,
        quantity=quantity,
        entry_id=555,
        submission_guid="test-guid",
        submission_timestamp=NOW,
        external_session=FakeSession(),
    )


def make_mod(award=1, divisor=1_000_000):
    return SimpleNamespace(
        id=1,
        group_id=GROUP_ID,
        item_id=ITEM_ID,
        npc_id=None,
        event_type="drop",
        award=award,
        divisor=divisor,
    )


class TestDefaultDropRuleFloors:
    async def test_below_the_divisor_awards_nothing(self, monkeypatch):
        result = await run(monkeypatch, value=800_000)
        assert result["receiver_points_awarded"] == 0
        assert result["total_points_awarded"] == 0

    async def test_just_over_half_awards_nothing(self, monkeypatch):
        # The half-up formula paid 1 here; the threshold was never met.
        result = await run(monkeypatch, value=500_000)
        assert result["receiver_points_awarded"] == 0

    async def test_exact_multiple_awards_in_full(self, monkeypatch):
        result = await run(monkeypatch, value=1_000_000)
        assert result["receiver_points_awarded"] == 1

    async def test_remainder_is_dropped_not_rounded(self, monkeypatch):
        # 21.7m at 1 per 1m = 21 points, not 22.
        result = await run(monkeypatch, value=21_700_000)
        assert result["receiver_points_awarded"] == 21

    async def test_award_multiplies_the_floored_quotient(self, monkeypatch):
        # 5 points per 1m: 1.8m = 1 × 5, not 2 × 5.
        result = await run(monkeypatch, value=1_800_000, point_config="5,1000000")
        assert result["receiver_points_awarded"] == 5


class TestStackedDropsFloor:
    async def test_stacked_total_floors(self, monkeypatch):
        # Stacking on: the stack's full value goes through the divisor once.
        result = await run(monkeypatch, value=1_800_000, quantity=3,
                           stacks_award_points=True)
        assert result["receiver_points_awarded"] == 1

    async def test_unstacked_per_item_floors(self, monkeypatch):
        # Stacking off: each 600k item is below the threshold, so nothing pays.
        result = await run(monkeypatch, value=1_800_000, quantity=3,
                           stacks_award_points=False)
        assert result["receiver_points_awarded"] == 0

    async def test_unstacked_per_item_over_threshold_pays_each(self, monkeypatch):
        # Each item is 1.5m → 1 point apiece, remainders dropped per item.
        result = await run(monkeypatch, value=4_500_000, quantity=3,
                           stacks_award_points=False)
        assert result["receiver_points_awarded"] == 3


class TestOverrideDivisorFloors:
    async def test_override_without_quantity_floors(self, monkeypatch):
        result = await run(monkeypatch, value=800_000, quantity=None,
                           mods=[make_mod()])
        assert result["receiver_points_awarded"] == 0

    async def test_override_without_quantity_pays_whole_multiples(self, monkeypatch):
        result = await run(monkeypatch, value=2_400_000, quantity=None,
                           mods=[make_mod()])
        assert result["receiver_points_awarded"] == 2
