"""Regression tests for the ``points_require_group_only`` gate in
``_check_and_award_points`` (``data/submissions/point_awards.py``).

Contract (user-reported fix, 2026-08-25): "group content" means a
split-eligible source. The plugin's ``nearby_players`` is a proximity scan,
not a party roster, so bystanders at solo content (pickpockets, slayer) must
not zero the receiver's award. When ``force_no_split`` is set — by the group's
own ``no_split`` rule or the global split-source policy — the drop collapses
to a full solo award even with non-members nearby. At genuinely
split-eligible sources the gate still blocks awards unless at least one other
group member is present.
"""

import json
from types import SimpleNamespace

import data.submissions.common as common
import data.submissions.point_awards as pa
import utils.split_policy as split_policy

NOW = 1_700_000_000
GROUP_ID = 42
NPC_ID = 8061

RECEIVER_ID = 1
PLAYERS = {
    RECEIVER_ID: "Receiver",
    2: "Clanmate",
    3: "Outsider",
}
NAME_TO_ID = {name.lower(): pid for pid, name in PLAYERS.items()}
GROUP_MEMBER_IDS = {RECEIVER_ID, 2}


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


def _patch_pipeline(monkeypatch, *, group_no_split=False, npc_split_eligible=True):
    """Stub the external dependencies of _check_and_award_points with
    require_group_only ON, sharing ON, and clan membership limited to
    GROUP_MEMBER_IDS. Returns the list of (player_id, points) paid out.
    """
    awards = []

    monkeypatch.setattr(common, "check_group_point_system_active",
                        lambda group_id, external_session=None: True)

    async def fake_point_config(group_id, external_session=None):
        return {"drop": "1,1000000"}

    async def fake_list_rules(group_id=None, item_id=None, npc_id=None,
                              external_session=None):
        return True, group_no_split

    async def fake_mods(group_id, external_session=None):
        return None

    async def fake_stack_config(group_id, external_session=None):
        return False

    async def fake_split_capability(group_id, player_id, external_session=None):
        return int(player_id) in GROUP_MEMBER_IDS

    async def fake_group_only(group_id, external_session=None):
        return True

    async def fake_limits(group_id, external_session=None):
        return 0, 0

    async def fake_sharing(group_id, external_session=None):
        return True, "equal_split"

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
    monkeypatch.setattr(split_policy, "allows_split",
                        lambda npc_id, npc_name=None, session=None: npc_split_eligible)
    return awards


async def run(monkeypatch, *, participants, **patch_kwargs):
    awards = _patch_pipeline(monkeypatch, **patch_kwargs)
    result = await pa._check_and_award_points(
        "drop",
        GROUP_ID,
        RECEIVER_ID,
        21_700_000,  # ≈ 22 pts at 1 per 1m
        players_included=json.dumps(participants),
        item_id=13652,
        npc_id=NPC_ID,
        quantity=1,
        entry_id=555,
        submission_guid="test-guid",
        submission_timestamp=NOW,
        external_session=FakeSession(),
    )
    return awards, result


class TestRequireGroupOnlyWithForcedNoSplit:
    async def test_policy_blocked_source_pays_receiver_despite_bystanders(self, monkeypatch):
        # The regression: pickpocket-style source (not split-eligible) with a
        # random passerby nearby must still pay the receiver in full.
        awards, result = await run(
            monkeypatch,
            participants=["Random Guy"],
            npc_split_eligible=False,
        )
        assert dict(awards) == {RECEIVER_ID: 22}
        assert result["receiver_points_awarded"] == 22

    async def test_policy_blocked_source_with_registered_outsider(self, monkeypatch):
        # Same, but the bystander resolves to a real player outside the group.
        awards, _ = await run(
            monkeypatch,
            participants=["Outsider"],
            npc_split_eligible=False,
        )
        assert dict(awards) == {RECEIVER_ID: 22}

    async def test_group_no_split_rule_pays_receiver_despite_bystanders(self, monkeypatch):
        # The group's own no_split list entry must behave the same way.
        awards, _ = await run(
            monkeypatch,
            participants=["Outsider"],
            group_no_split=True,
            npc_split_eligible=True,
        )
        assert dict(awards) == {RECEIVER_ID: 22}


class TestRequireGroupOnlyAtSplitEligibleSources:
    async def test_group_content_without_members_awards_nothing(self, monkeypatch):
        # Split-eligible source, only outsiders present: the gate holds.
        awards, result = await run(
            monkeypatch,
            participants=["Outsider"],
            npc_split_eligible=True,
        )
        assert awards == []
        assert result["receiver_points_awarded"] == 0
        assert result["total_points_awarded"] == 0

    async def test_group_content_with_member_splits_normally(self, monkeypatch):
        # One clanmate present: 22 pts split equally, 11 each.
        awards, _ = await run(
            monkeypatch,
            participants=["Clanmate", "Outsider"],
            npc_split_eligible=True,
        )
        assert dict(awards) == {2: 11, RECEIVER_ID: 11}

    async def test_solo_kill_unaffected(self, monkeypatch):
        awards, _ = await run(
            monkeypatch,
            participants=[],
            npc_split_eligible=True,
        )
        assert dict(awards) == {RECEIVER_ID: 22}
