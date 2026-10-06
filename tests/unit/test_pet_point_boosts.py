"""A timed point boost that lists a pet must double that pet.

Incident 2026-10-05, group 190. Boost #13 (x2, 2026-10-01..10-09) was a
``drop`` boost targeting raid uniques by item id, including Olmlet, Lil' zik
and Tumeken's guardian. Two Lil' zik pets landed in the window and both paid
the flat 50 instead of 100; the clan's admins topped each up by hand.

Two independent gaps, either one enough to miss:

1. Pets never arrive as drops. ``pet.py`` awards them under reason ``pet``,
   so a ``drop`` boost's event-type filter skipped them outright.
2. ``pet.py`` called ``check_and_award_points`` without ``item_id`` or
   ``npc_id``, so no item-targeted boost (of any event type) could match.

The fix lets an item-targeted ``drop`` boost cover pets it lists, and has
``pet.py`` pass the pet's item and source NPC through. NPC- and any-target
``drop`` boosts still do not touch pets: "x2 on ToB drops" is not a statement
about the pet, but a pet item picked into the target list is.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from tests.unit.test_point_boost_engine import FakeSession, NOW, make_boost

from data.submissions.point_awards import modify_for_event

LIL_ZIK = 22473
OLMLET = 20851
TOB_NPC = 12447
GROUP_190_RAID_BOOST = [25742, 25746, 25744, LIL_ZIK, 24670, OLMLET, 27352, 22386]


async def run_pet(boosts, default_value=50, **kwargs):
    kwargs.setdefault("submission_timestamp", NOW)
    return await modify_for_event(
        "pet", 190, 7, default_value, external_session=FakeSession(boosts), **kwargs,
    )


class TestDropBoostCoversListedPets:
    async def test_incident_boost_doubles_lil_zik(self):
        boost = make_boost(event_type="drop", target_type="item", target_ids=GROUP_190_RAID_BOOST)
        assert await run_pet([boost], item_id=LIL_ZIK) == 100

    async def test_single_target_drop_boost_on_a_pet(self):
        boost = make_boost(event_type="drop", target_type="item", target_id=OLMLET)
        assert await run_pet([boost], item_id=OLMLET) == 100

    async def test_pet_not_in_the_list_is_unboosted(self):
        boost = make_boost(event_type="drop", target_type="item", target_ids=GROUP_190_RAID_BOOST)
        assert await run_pet([boost], item_id=13071) == 50  # Chompy chick

    async def test_pet_without_item_id_is_unboosted(self):
        boost = make_boost(event_type="drop", target_type="item", target_ids=GROUP_190_RAID_BOOST)
        assert await run_pet([boost], item_id=None) == 50

    async def test_npc_targeted_drop_boost_leaves_pets_alone(self):
        boost = make_boost(event_type="drop", target_type="npc", target_id=TOB_NPC)
        assert await run_pet([boost], item_id=LIL_ZIK, npc_id=TOB_NPC) == 50

    async def test_any_target_drop_boost_leaves_pets_alone(self):
        assert await run_pet([make_boost(event_type="drop")], item_id=LIL_ZIK) == 50

    async def test_other_event_types_still_do_not_cross_over(self):
        boost = make_boost(event_type="pb", target_type="item", target_id=LIL_ZIK)
        assert await run_pet([boost], item_id=LIL_ZIK) == 50

    async def test_item_drop_boost_does_not_reach_clog(self):
        boost = make_boost(event_type="drop", target_type="item", target_id=LIL_ZIK)
        result = await modify_for_event(
            "clog", 190, 7, 1, item_id=LIL_ZIK,
            submission_timestamp=NOW, external_session=FakeSession([boost]),
        )
        assert result == 1


class TestPetBoostsNowMatchByItem:
    async def test_pet_boost_targeting_the_item(self):
        boost = make_boost(event_type="pet", target_type="item", target_id=LIL_ZIK)
        assert await run_pet([boost], item_id=LIL_ZIK) == 100

    async def test_pet_boost_targeting_the_source_npc(self):
        boost = make_boost(event_type="pet", target_type="npc", target_id=TOB_NPC)
        assert await run_pet([boost], npc_id=TOB_NPC) == 100


def _session(player):
    session = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.filter_by.return_value = q
        q.order_by.return_value = q
        q.first.return_value = player if model is type(player) else None
        q.all.return_value = []
        return q

    session.query.side_effect = _query
    return session


class _FakePlayer:
    player_id = 5752782
    user_id = 99
    user = MagicMock()


def _drive_pet_processor(payload, item_id=LIL_ZIK, npc=(TOB_NPC, "Theatre of Blood")):
    import data.submissions.pet as pet

    player = _FakePlayer()
    session = _session(player)
    award = AsyncMock(return_value={
        "receiver_points_awarded": 0, "receiver_current_points": 0,
        "total_points_awarded": 0, "awarded_members": [],
    })
    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(pet, name, **kw))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_player_by_name_then_auth", new=AsyncMock(return_value=(player, True, True)))
        p("ensure_item_by_name", new=AsyncMock(
            return_value=MagicMock(item_id=item_id) if item_id else None))
        p("ensure_npc_id_for_player", new=AsyncMock(return_value=npc))
        for name, value in [
            ("ensure_can_create", AsyncMock(return_value=True)),
            ("get_player_groups_with_global", MagicMock(return_value=[MagicMock(group_id=190)])),
            ("screenshot_required", AsyncMock(return_value=False)),
            ("create_notification", AsyncMock()),
            ("is_user_dm_enabled", MagicMock(return_value=False)),
            ("get_config_prefix", MagicMock(return_value="")),
            ("award_points_to_player", MagicMock()),
        ]:
            if hasattr(pet, name):
                p(name, new=value)
        stack.enter_context(
            patch("data.submissions.common.check_group_point_system_active", return_value=True)
        )
        stack.enter_context(
            patch("data.submissions.point_awards.check_and_award_points", new=award)
        )
        stack.enter_context(patch("utils.group_config.get", return_value="false"))
        stack.enter_context(patch("utils.group_config.is_truthy", return_value=False))
        asyncio.run(pet.pet_processor(payload, external_session=session))
    return award


PAYLOAD = {
    "player_name": "D CAV", "acc_hash": "-537678130437333791",
    "pet_name": "Lil' zik", "guid": "pet-boost-guid", "source": "Theatre of Blood",
    "p_v": "6.0.4",
}


class TestPetProcessorPassesIds:
    def test_award_receives_pet_item_and_source_npc(self):
        award = _drive_pet_processor(PAYLOAD)
        assert award.await_count == 1, "the pet never reached the point pass"
        args, kwargs = award.await_args
        assert args[0] == "pet"
        assert kwargs["item_id"] == LIL_ZIK
        assert kwargs["npc_id"] == TOB_NPC

    def test_skilling_pet_passes_no_npc(self):
        payload = dict(PAYLOAD, pet_name="Heron", source=None, guid="heron-guid")
        award = _drive_pet_processor(payload, item_id=13320, npc=(None, None))
        assert award.await_count == 1
        _, kwargs = award.await_args
        assert kwargs["item_id"] == 13320
        assert kwargs["npc_id"] is None
