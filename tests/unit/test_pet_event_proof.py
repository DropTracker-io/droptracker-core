"""Pet screenshots reach events as proof, duplicates included (2026-11-21).

The plugin screenshots every pet roll, but the processor only downloaded the
image for a pet DropTracker had never seen, and the event envelope never
carried an image at all: every pet event completion had no proof, and a
duplicate's screenshot was thrown away entirely. Now every roll's attachment
is stored and handed to the event engine as ``image_url``.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import utils.pet_notifications as pn

STORED = "https://video.droptracker.io/dt_img/user-upload/1/pet/pet_x.png"


class _FakePlayer:
    player_id = 5752901
    user_id = 99
    user = MagicMock()
    player_name = "I Am Papii"
    wom_id = 2402887


def _session(player, owned_pet):
    session = MagicMock()

    def _query(model):
        q = MagicMock()
        q.filter.return_value = q
        q.filter_by.return_value = q
        q.order_by.return_value = q
        if model is type(player):
            q.first.return_value = player
        elif "Pet" in getattr(model, "__name__", ""):
            q.first.return_value = owned_pet
        else:
            q.first.return_value = None
        q.all.return_value = []
        return q

    session.query.side_effect = _query
    return session


def _drive(payload, owned_pet=None, replay=False):
    """Run pet_processor; return (download mock, queued event envelope data)."""
    import data.submissions.pet as pet

    player = _FakePlayer()
    session = _session(player, owned_pet)
    if replay:
        from sqlalchemy.exc import IntegrityError

        session.begin_nested.side_effect = IntegrityError("dup", {}, Exception())
    download = AsyncMock(return_value=("/tmp/sentinel", STORED))
    queued = MagicMock()
    with ExitStack() as stack:
        p = lambda name, **kw: stack.enter_context(patch.object(pet, name, **kw))
        p("_new_to_collection_log", new=MagicMock(return_value=False))
        p("select_session_and_flag", new=MagicMock(return_value=(session, True)))
        p("ensure_player_by_name_then_auth", new=AsyncMock(return_value=(player, True, True)))
        p("ensure_item_by_name", new=AsyncMock(return_value=MagicMock(item_id=30888)))
        p("ensure_npc_id_for_player", new=AsyncMock(return_value=(14176, "Yama")))
        p("ensure_can_create", new=AsyncMock(return_value=True))
        p("get_player_groups_with_global", new=MagicMock(return_value=[]))
        p("screenshot_required", new=AsyncMock(return_value=False))
        p("create_notification", new=AsyncMock())
        p("is_user_dm_enabled", new=MagicMock(return_value=False))
        p("get_config_prefix", new=MagicMock(return_value=""))
        stack.enter_context(patch("data.submissions.common.download_player_image", new=download))
        stack.enter_context(patch("data.submissions.common.get_extension_from_content_type",
                                  return_value="png"))
        stack.enter_context(
            patch("data.submissions.common.check_group_point_system_active", return_value=False))
        stack.enter_context(patch("utils.group_config.get_effective", return_value="1"))
        stack.enter_context(patch.object(pn, "load_settings", return_value=("all", ())))
        stack.enter_context(patch("services.realtime.publish_feed_pet", create=True))
        # `services` is a stub package in unit tests, so the processor's lazy
        # `from services.event_engine import queue_submission` needs a real
        # module entry to land on (it swallows the ImportError otherwise).
        stack.enter_context(patch.dict(sys.modules, {
            "services.event_engine": SimpleNamespace(queue_submission=queued)}))
        asyncio.run(pet.pet_processor(payload, external_session=session))
    assert queued.call_count == 1
    return download, queued.call_args.args[3]


PAYLOAD = {
    "player_name": "I Am Papii", "acc_hash": "-7115709280228222040",
    "pet_name": "Yami", "guid": "pet-proof-guid", "source": "Yama",
    "attachment_url": "https://cdn.discordapp.com/attachments/1/2/image.png",
    "attachment_type": "image/png",
}


class TestPetProof:
    def test_new_pet_screenshot_reaches_the_event(self):
        download, data = _drive(dict(PAYLOAD, duplicate="false"))
        assert download.await_count == 1
        assert data["is_new_pet"] is True
        assert data["image_url"] == STORED

    def test_duplicate_screenshot_is_kept_and_reaches_the_event(self):
        owned = SimpleNamespace(id=5615)
        download, data = _drive(dict(PAYLOAD, duplicate="true"), owned_pet=owned)
        assert download.await_count == 1
        assert data["is_new_pet"] is False
        assert data["image_url"] == STORED

    def test_no_attachment_sends_no_proof(self):
        payload = {k: v for k, v in PAYLOAD.items() if not k.startswith("attachment")}
        download, data = _drive(payload)
        assert download.await_count == 0
        assert data["image_url"] is None

    def test_already_downloaded_image_is_passed_through(self):
        download, data = _drive(dict(PAYLOAD, downloaded=True, image_url=STORED))
        assert download.await_count == 0
        assert data["image_url"] == STORED

    def test_replay_does_not_store_a_second_copy(self):
        download, _data = _drive(dict(PAYLOAD), replay=True)
        assert download.await_count == 0
