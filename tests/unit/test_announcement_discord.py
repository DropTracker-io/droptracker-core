"""A group announcement's Discord post follows the website.

Editing used to change only the site and archiving left the post up; a post
whose clan had no announcements channel published and went nowhere without a
word. These pin the sync decisions, the drain's new ``edit_announcement``
kind, and the admin list's Discord state.
"""
import asyncio
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ad = _load("_announcement_discord_ut", "services", "announcement_discord.py")
outbox = _load("_outbox_announcement_ut", "services", "discord_outbox.py")


def _ann(**kw):
    base = dict(id=7, scope_type="group", discord_message_id=None, discord_channel_id=None,
                post_to_discord=True, status="published")
    base.update(kw)
    return SimpleNamespace(**base)


def _row(status, **kw):
    base = dict(status=status, channel_id="111", discord_message_id=None, error=None,
                embed_json=json.dumps({"title": "Old", "description": "old body", "color": 255,
                                       "footer": {"text": "Staff"}}))
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def wired(monkeypatch):
    """Sync with the outbox's enqueue recorded and the send rows faked."""
    queued = []
    fake_outbox = SimpleNamespace(enqueue=lambda s, **kw: queued.append(kw))
    monkeypatch.setitem(sys.modules, "services.discord_outbox", fake_outbox)
    rows = []
    monkeypatch.setattr(ad, "_send_rows", lambda s, ann_id: rows)
    return queued, rows


class TestPatch:
    def test_only_changed_fields(self):
        assert ad.embed_patch(title="T") == {"title": "T"}
        assert ad.embed_patch(body_md="b" * 5000) == {"description": "b" * 4000}
        assert ad.embed_patch() == {}

    def test_image_set_and_removed(self):
        assert ad.embed_patch(cover_image_url="https://x/i.png") == {"image": {"url": "https://x/i.png"}}
        assert ad.embed_patch(cover_image_url=None) == {"image": None}

    def test_apply_keeps_the_rest_of_the_embed(self):
        embed = {"title": "Old", "color": 255, "footer": {"text": "Staff"}, "image": {"url": "a"}}
        out = ad.apply_patch(embed, {"title": "New", "image": None})
        assert out == {"title": "New", "color": 255, "footer": {"text": "Staff"}}


class TestEdit:
    def test_waiting_post_is_rewritten_in_place(self, wired):
        queued, rows = wired
        rows.append(_row("pending"))
        assert ad.sync_edit(None, _ann(), {"title": "New"}) == "updated_pending"
        assert queued == []
        embed = json.loads(rows[0].embed_json)
        assert embed["title"] == "New" and embed["color"] == 255

    def test_posted_message_gets_an_edit_row(self, wired):
        queued, rows = wired
        rows.append(_row("sent", discord_message_id="999"))
        ann = _ann(discord_message_id="999", discord_channel_id="111")
        assert ad.sync_edit(None, ann, {"description": "new"}) == "queued"
        assert queued[0]["kind"] == "edit_announcement"
        assert queued[0]["discord_message_id"] == "999"
        assert queued[0]["channel_id"] == "111"
        assert queued[0]["embed"] == {"description": "new"}
        assert queued[0]["commit"] is False  # lands with the edit itself

    def test_in_flight_post_queues_an_unresolved_edit(self, wired):
        # The drain fills the id from the announcement once the send writes it back.
        queued, rows = wired
        rows.append(_row("sending"))
        assert ad.sync_edit(None, _ann(), {"title": "x"}) == "queued"
        assert queued[0]["discord_message_id"] is None

    def test_failed_or_never_posted_is_left_alone(self, wired):
        queued, rows = wired
        assert ad.sync_edit(None, _ann(), {"title": "x"}) == "none"
        rows.append(_row("failed"))
        assert ad.sync_edit(None, _ann(), {"title": "x"}) == "none"
        assert queued == []

    def test_global_and_empty_patches_do_nothing(self, wired):
        queued, rows = wired
        rows.append(_row("sent", discord_message_id="9"))
        assert ad.sync_edit(None, _ann(scope_type="global", discord_message_id="9"), {"title": "x"}) == "none"
        assert ad.sync_edit(None, _ann(discord_message_id="9"), {}) == "none"
        assert queued == []


class TestArchive:
    def test_waiting_post_is_cancelled_not_sent(self, wired):
        queued, rows = wired
        rows.append(_row("pending"))
        assert ad.sync_archive(None, _ann()) == "cancelled"
        assert rows[0].status == "cancelled"
        assert queued == []

    def test_posted_message_is_deleted(self, wired):
        queued, rows = wired
        rows.append(_row("sent", discord_message_id="999"))
        assert ad.sync_archive(None, _ann(discord_message_id="999", discord_channel_id="111")) == "queued"
        assert queued[0]["kind"] == "delete_own_message"  # never someone else's message
        assert queued[0]["discord_message_id"] == "999"

    def test_nothing_posted(self, wired):
        queued, rows = wired
        assert ad.sync_archive(None, _ann()) == "none"
        assert queued == []


class TestState:
    def test_states(self):
        assert ad.state_for(_ann(discord_message_id="1"), []) == {"status": "posted"}
        assert ad.state_for(_ann(), [_row("pending")]) == {"status": "sending"}
        assert ad.state_for(_ann(), [_row("failed", error="403 Forbidden: Missing Permissions")]) == {
            "status": "failed", "reason": "missing_permissions"}
        assert ad.state_for(_ann(), [_row("failed", error="404 Unknown Channel")]) == {
            "status": "failed", "reason": "channel_missing"}
        assert ad.state_for(_ann(post_to_discord=False), []) == {"status": "off"}
        # Asked for Discord, never queued: the clan had no channel back then.
        assert ad.state_for(_ann(), []) == {"status": "not_sent"}

    def test_reason_never_carries_raw_text(self):
        assert ad.classify_error("some <@123> internal detail") == "error"


class TestDrainEdit:
    """The bot applies the patch to the posted embed, keeping the rest."""

    def _drain_row(self, row, message, monkeypatch, ann=None):
        fake_interactions = MagicMock()
        fake_interactions.Embed.from_dict = staticmethod(lambda d: {"embed": d})
        monkeypatch.setitem(sys.modules, "interactions", fake_interactions)
        monkeypatch.setitem(sys.modules, "services.announcement_discord", ad)
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = ann
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)
        asyncio.run(outbox._edit_announcement(bot, session, row))
        return channel

    def _message(self):
        message = MagicMock()
        embed = MagicMock()
        embed.to_dict.return_value = {"title": "Old", "description": "old", "color": 255,
                                      "footer": {"text": "Staff"}}
        message.embeds = [embed]
        message.edit = AsyncMock()
        return message

    def test_patch_applied_over_the_posted_embed(self, monkeypatch):
        message = self._message()
        row = SimpleNamespace(channel_id="111", discord_message_id="999", ref_type="announcement",
                              ref_id=7, error=None, embed_json=json.dumps({"title": "New"}))
        self._drain_row(row, message, monkeypatch)
        sent = message.edit.call_args.kwargs["embeds"][0]["embed"]
        assert sent == {"title": "New", "description": "old", "color": 255, "footer": {"text": "Staff"}}

    def test_unresolved_id_comes_from_the_announcement(self, monkeypatch):
        message = self._message()
        row = SimpleNamespace(channel_id="111", discord_message_id=None, ref_type="announcement",
                              ref_id=7, error=None, embed_json=json.dumps({"title": "New"}))
        ann = SimpleNamespace(discord_message_id="555", discord_channel_id="222")
        channel = self._drain_row(row, message, monkeypatch, ann=ann)
        assert row.discord_message_id == "555" and row.channel_id == "222"
        channel.fetch_message.assert_awaited_with(555)

    def test_never_posted_is_a_no_op(self, monkeypatch):
        row = SimpleNamespace(channel_id="111", discord_message_id=None, ref_type="announcement",
                              ref_id=7, error=None, embed_json="{}")
        ann = SimpleNamespace(discord_message_id=None, discord_channel_id=None)
        self._drain_row(row, self._message(), monkeypatch, ann=ann)
        assert row.error == "no Discord message to edit"

    def test_message_deleted_by_hand_counts_as_done(self, monkeypatch):
        row = SimpleNamespace(channel_id="111", discord_message_id="999", ref_type="announcement",
                              ref_id=7, error=None, embed_json="{}")
        fake_interactions = MagicMock()
        monkeypatch.setitem(sys.modules, "interactions", fake_interactions)
        monkeypatch.setitem(sys.modules, "services.announcement_discord", ad)
        channel = MagicMock()
        channel.fetch_message = AsyncMock(side_effect=Exception("404 Unknown Message"))
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)
        asyncio.run(outbox._edit_announcement(bot, MagicMock(), row))
        assert row.error.startswith("message gone")


class TestDrainDelete:
    def test_unposted_announcement_delete_is_a_no_op(self):
        row = SimpleNamespace(channel_id="111", discord_message_id=None, ref_type="announcement",
                              ref_id=7, error=None)
        session = MagicMock()
        session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            discord_message_id=None, discord_channel_id=None)
        asyncio.run(outbox._delete_message(MagicMock(), session, row, own_only=True))
        assert row.error == "no Discord message to delete"

    def test_other_deletes_still_require_an_id(self):
        row = SimpleNamespace(channel_id="111", discord_message_id=None, ref_type="drop",
                              ref_id=7, error=None)
        with pytest.raises(RuntimeError):
            asyncio.run(outbox._delete_message(MagicMock(), MagicMock(), row))
