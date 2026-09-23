"""Regression tests for how ``add_per_member`` boosts interact with point
splitting in ``_check_and_award_points`` (``data/submissions/point_awards.py``).

Contract (user-reported fix, 2026-08-12): the TOTAL boost injected into one
submission is always ``value × members present``. With sharing on, the split is
computed on the un-boosted award and each recipient's share then gets ``+value``
once (Dex 21.7m ≈ 22 pts, 2 present, boost 20 → 11 + 20 = 31 each, NOT
11 + 20×2 = 51 each). Without a split, the receiver banks the whole
``value × N``. A zero floor-share still receives an active boost.
"""

import json
from types import SimpleNamespace

import data.submissions.common as common
import data.submissions.point_awards as pa

NOW = 1_700_000_000
GROUP_ID = 42

RECEIVER_ID = 1
PLAYERS = {
    RECEIVER_ID: "Receiver",
    2: "Partner One",
    3: "Partner Two",
}
NAME_TO_ID = {name.lower(): pid for pid, name in PLAYERS.items()}


def make_boost(operation="add_per_member", operation_value=20, boost_id=1):
    return SimpleNamespace(
        id=boost_id,
        group_id=GROUP_ID,
        start_time_unix=NOW - 3600,
        end_time_unix=NOW + 3600,
        event_type="any",
        target_type="any",
        target_id=None,
        target_ids=None,
        operation=operation,
        operation_value=operation_value,
    )


class FakeSession:
    """Serves boost rows to modify_for_event; inert for everything else."""

    def __init__(self, boosts):
        self._boosts = boosts

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return sorted(self._boosts, key=lambda b: b.id, reverse=True)

    def first(self):
        return None


def _patch_pipeline(monkeypatch, *, sharing, split_method="equal_split",
                    point_config="1,1000000", limits=(0, 0)):
    """Stub every external dependency of _check_and_award_points except the
    boost engine (modify_for_event) and the split math, which stay real.

    Returns the list of (player_id, points) actually paid out.
    """
    awards = []

    monkeypatch.setattr(common, "check_group_point_system_active",
                        lambda group_id, external_session=None: True)

    async def fake_point_config(group_id, external_session=None):
        return {"drop": point_config}

    async def fake_list_rules(group_id=None, item_id=None, npc_id=None,
                              external_session=None):
        return True, False

    async def fake_mods(group_id, external_session=None):
        return None

    async def fake_stack_config(group_id, external_session=None):
        return False

    async def fake_split_capability(group_id, player_id, external_session=None):
        return True

    async def fake_group_only(group_id, external_session=None):
        return False

    async def fake_limits(group_id, external_session=None):
        return limits

    async def fake_sharing(group_id, external_session=None):
        return sharing, split_method

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
                        lambda player_id, session: PLAYERS.get(int(player_id)))
    monkeypatch.setattr(pa, "_get_player_id_by_name",
                        lambda name, session: NAME_TO_ID.get(str(name).lower()))
    return awards


async def run(monkeypatch, *, boosts, value, participants, sharing,
              split_method="equal_split", **patch_kwargs):
    awards = _patch_pipeline(monkeypatch, sharing=sharing,
                             split_method=split_method, **patch_kwargs)
    result = await pa._check_and_award_points(
        "drop",
        GROUP_ID,
        RECEIVER_ID,
        value,
        players_included=json.dumps(participants),
        item_id=13652,
        npc_id=None,
        quantity=1,
        entry_id=555,
        submission_guid="test-guid",
        submission_timestamp=NOW,
        external_session=FakeSession(boosts),
    )
    return awards, result


class TestAddPerMemberWithSharing:
    async def test_dex_example_boost_added_once_per_share(self, monkeypatch):
        # 21.7m Dex ≈ 22 pts, 2 members present, boost 20/member:
        # equal split 11/11, +20 each → 31 each, total 62 = 22 + 20×2.
        awards, result = await run(
            monkeypatch,
            boosts=[make_boost(operation_value=20)],
            value=21_700_000,
            participants=["Partner One"],
            sharing=True,
        )
        assert dict(awards) == {2: 31, RECEIVER_ID: 31}
        assert result["receiver_points_awarded"] == 31
        assert result["total_points_awarded"] == 62

    async def test_award_all_boost_added_once_each(self, monkeypatch):
        awards, _ = await run(
            monkeypatch,
            boosts=[make_boost(operation_value=20)],
            value=21_700_000,
            participants=["Partner One"],
            sharing=True,
            split_method="award_all",
        )
        assert dict(awards) == {2: 42, RECEIVER_ID: 42}

    async def test_zero_floor_share_still_receives_boost(self, monkeypatch):
        # Award 1, three present: shares 0/0, receiver keeps the remainder 1.
        # The boost must still lift the zero shares: 20/20 and 21.
        awards, result = await run(
            monkeypatch,
            boosts=[make_boost(operation_value=20)],
            value=1_000_000,
            participants=["Partner One", "Partner Two"],
            sharing=True,
        )
        assert dict(awards) == {2: 20, 3: 20, RECEIVER_ID: 21}
        assert result["total_points_awarded"] == 61  # 1 + 20×3

    async def test_no_boost_split_unchanged(self, monkeypatch):
        awards, _ = await run(
            monkeypatch,
            boosts=[],
            value=21_700_000,
            participants=["Partner One"],
            sharing=True,
        )
        assert dict(awards) == {2: 11, RECEIVER_ID: 11}

    async def test_no_boost_zero_share_awards_nothing(self, monkeypatch):
        # Guard regression: with no active boost, a zero floor-share must not
        # produce an award row.
        awards, _ = await run(
            monkeypatch,
            boosts=[],
            value=1_000_000,
            participants=["Partner One", "Partner Two"],
            sharing=True,
        )
        assert dict(awards) == {RECEIVER_ID: 1}


class TestAddPerMemberWithoutSharing:
    async def test_receiver_banks_full_boost_when_sharing_off(self, monkeypatch):
        # No split happens, so the receiver keeps base + value × N: 22 + 40.
        awards, result = await run(
            monkeypatch,
            boosts=[make_boost(operation_value=20)],
            value=21_700_000,
            participants=["Partner One"],
            sharing=False,
        )
        assert dict(awards) == {RECEIVER_ID: 62}
        assert result["receiver_points_awarded"] == 62

    async def test_solo_gets_single_boost(self, monkeypatch):
        awards, _ = await run(
            monkeypatch,
            boosts=[make_boost(operation_value=20)],
            value=21_700_000,
            participants=[],
            sharing=True,
        )
        assert dict(awards) == {RECEIVER_ID: 42}
