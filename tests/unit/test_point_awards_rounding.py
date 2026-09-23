"""Regression tests for GP-threshold rounding in ``_check_and_award_points``
(``data/submissions/point_awards.py``).

Contract (user-reported fix, 2026-09-23): a group's drop divisor is a MINIMUM
as well as a rate. A drop worth less than the divisor has not met the bar the
clan set, so it earns nothing — half-up division used to pay a full point for
anything from 500k up under "1 point per 1m", which handed out points below
that minimum. Once the bar IS met the remainder still rounds half-up exactly
as before: 1.6m is 2 points, 21.7m is 22. Only the sub-threshold case changed.

Every GP path applies the same rule — the default drop rule, stacked and
unstacked quantities, and a per-item/NPC override with a divisor.
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


class TestBelowTheMinimum:
    """The only behaviour that changed: a drop under the divisor pays nothing."""

    async def test_reported_case_800k_awards_nothing(self, monkeypatch):
        result = await run(monkeypatch, value=800_000)
        assert result["receiver_points_awarded"] == 0
        assert result["total_points_awarded"] == 0

    async def test_exactly_half_the_divisor_awards_nothing(self, monkeypatch):
        # Half-up paid 1 from here up; the clan's minimum was never met.
        result = await run(monkeypatch, value=500_000)
        assert result["receiver_points_awarded"] == 0

    async def test_one_gp_short_awards_nothing(self, monkeypatch):
        result = await run(monkeypatch, value=999_999)
        assert result["receiver_points_awarded"] == 0

    async def test_exactly_the_divisor_pays(self, monkeypatch):
        result = await run(monkeypatch, value=1_000_000)
        assert result["receiver_points_awarded"] == 1

    async def test_high_divisor_only_gates_below_itself(self, monkeypatch):
        # 5 per 10m: 9.9m is under the bar, 10m clears it and pays the award.
        assert (await run(monkeypatch, value=9_900_000,
                          point_config="5,10000000"))["receiver_points_awarded"] == 0
        assert (await run(monkeypatch, value=10_000_000,
                          point_config="5,10000000"))["receiver_points_awarded"] == 5


class TestAboveTheMinimumStillRounds:
    """Everything at or above the divisor rounds half-up, exactly as before."""

    async def test_remainder_over_half_rounds_up(self, monkeypatch):
        result = await run(monkeypatch, value=1_600_000)
        assert result["receiver_points_awarded"] == 2

    async def test_remainder_under_half_rounds_down(self, monkeypatch):
        result = await run(monkeypatch, value=1_400_000)
        assert result["receiver_points_awarded"] == 1

    async def test_remainder_at_half_rounds_up(self, monkeypatch):
        result = await run(monkeypatch, value=1_500_000)
        assert result["receiver_points_awarded"] == 2

    async def test_dex_example_unchanged(self, monkeypatch):
        # 21.7m at 1 per 1m stays 22 points, as it has always been.
        result = await run(monkeypatch, value=21_700_000)
        assert result["receiver_points_awarded"] == 22

    async def test_award_multiplies_the_rounded_quotient(self, monkeypatch):
        # 5 points per 1m: 1.8m rounds to 2, so 10 points.
        result = await run(monkeypatch, value=1_800_000, point_config="5,1000000")
        assert result["receiver_points_awarded"] == 10


class TestStackedDrops:
    async def test_stacked_total_clears_the_bar_and_rounds(self, monkeypatch):
        # Stacking on: the stack's full 1.8m goes through the divisor once.
        result = await run(monkeypatch, value=1_800_000, quantity=3,
                           stacks_award_points=True)
        assert result["receiver_points_awarded"] == 2

    async def test_unstacked_items_below_the_bar_pay_nothing(self, monkeypatch):
        # Stacking off: each item is 600k, under the minimum, so nothing pays
        # even though the stack is worth 1.8m.
        result = await run(monkeypatch, value=1_800_000, quantity=3,
                           stacks_award_points=False)
        assert result["receiver_points_awarded"] == 0

    async def test_unstacked_items_over_the_bar_round_per_item(self, monkeypatch):
        # Each item is 1.5m → rounds to 2 apiece → 6.
        result = await run(monkeypatch, value=4_500_000, quantity=3,
                           stacks_award_points=False)
        assert result["receiver_points_awarded"] == 6


class TestOverrideDivisor:
    async def test_override_below_the_bar_pays_nothing(self, monkeypatch):
        result = await run(monkeypatch, value=800_000, quantity=None,
                           mods=[make_mod()])
        assert result["receiver_points_awarded"] == 0

    async def test_override_above_the_bar_still_rounds(self, monkeypatch):
        result = await run(monkeypatch, value=2_600_000, quantity=None,
                           mods=[make_mod()])
        assert result["receiver_points_awarded"] == 3
