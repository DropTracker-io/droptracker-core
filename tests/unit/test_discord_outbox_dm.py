"""Direct messages through the outbox (web96a).

The Web API may never open a Discord connection, so the "you've been
challenged" DM is queued as a ``discord_outbox`` row and sent by the core
bot's drain. Three things about that branch matter:

* **``channel_id`` means a USER id for ``kind='dm'``.** The drain must resolve
  it with ``fetch_user``; treating it as a channel silently fails for every
  recipient.
* **A closed DM is not a failure.** Discord users routinely block DMs from
  server members. Marking those rows ``failed`` would retry the same bounce on
  every drain forever; they are recorded as sent with the reason, exactly as
  ``services/recap_delivery.py`` treats a bounced recap. The website inbox is
  the fallback.
* **Components are LINK buttons or nothing.** A URL button raises no
  interaction, which is what lets the outbox stay a stateless queue. A
  malformed entry is dropped rather than being allowed to fail the whole send.
"""
import asyncio
import importlib.util
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "discord_outbox.py",
)
_spec = importlib.util.spec_from_file_location("_outbox_dm_under_test", _MODULE_PATH)
_outbox = importlib.util.module_from_spec(_spec)
sys.modules["_outbox_dm_under_test"] = _outbox
_spec.loader.exec_module(_outbox)


def _row(**kw):
    row = MagicMock()
    row.kind = kw.get("kind", "dm")
    row.channel_id = kw.get("channel_id", "528746710042804247")
    row.content = kw.get("content", None)
    row.embed_json = kw.get("embed_json", json.dumps({"title": "Clan challenge"}))
    row.components_json = kw.get(
        "components_json",
        json.dumps([{"label": "Respond", "url": "https://www.droptracker.io/x"}]),
    )
    return row


class _FakeButton:
    def __init__(self, style=None, label=None, url=None):
        self.style = style
        self.label = label
        self.url = url


class _FakeActionRow:
    def __init__(self, *buttons):
        self.buttons = buttons


def _install_interactions(monkeypatch_target=None):
    """Substitute the (conftest-stubbed) interactions module with fakes we can
    assert on."""
    fake = MagicMock()
    fake.Button = _FakeButton
    fake.ActionRow = _FakeActionRow
    fake.ButtonStyle = MagicMock(LINK="link")
    fake.Embed.from_dict = staticmethod(lambda d: {"embed": d})
    sys.modules["interactions"] = fake
    return fake


class TestComponentBuilding(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("interactions")
        _install_interactions()

    def tearDown(self):
        if self._saved is not None:
            sys.modules["interactions"] = self._saved

    def test_builds_link_buttons(self):
        rows = _outbox._build_components(
            json.dumps([{"label": "Respond on DropTracker",
                         "url": "https://www.droptracker.io/groups/1/events/invitations/2"}])
        )
        self.assertEqual(len(rows), 1)
        button = rows[0].buttons[0]
        self.assertEqual(button.style, "link")
        self.assertEqual(button.label, "Respond on DropTracker")

    def test_drops_entries_without_an_http_url(self):
        """One malformed entry must not cost the whole message its send."""
        rows = _outbox._build_components(
            json.dumps(
                [
                    {"label": "Bad", "url": "javascript:alert(1)"},
                    {"label": "Also bad", "custom_id": "do_thing"},
                    {"label": "Good", "url": "https://www.droptracker.io/x"},
                ]
            )
        )
        self.assertEqual(len(rows[0].buttons), 1)
        self.assertEqual(rows[0].buttons[0].label, "Good")

    def test_no_components_for_empty_or_corrupt_json(self):
        for raw in (None, "", "[]", "{not json", json.dumps([{"label": "x"}])):
            self.assertIsNone(_outbox._build_components(raw))

    def test_caps_button_count(self):
        entries = [
            {"label": f"b{i}", "url": f"https://www.droptracker.io/{i}"}
            for i in range(10)
        ]
        rows = _outbox._build_components(json.dumps(entries))
        self.assertEqual(len(rows[0].buttons), _outbox._MAX_COMPONENT_BUTTONS)


class TestSendDM(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("interactions")
        _install_interactions()

    def tearDown(self):
        if self._saved is not None:
            sys.modules["interactions"] = self._saved

    def test_resolves_a_user_not_a_channel(self):
        user = MagicMock()
        user.send = AsyncMock(return_value=MagicMock(id=1))
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)
        bot.fetch_channel = AsyncMock(side_effect=AssertionError("must not be used"))

        asyncio.run(_outbox._send_dm(bot, MagicMock(), _row()))

        bot.fetch_user.assert_awaited_once_with(528746710042804247)
        kwargs = user.send.await_args.kwargs
        self.assertIn("embeds", kwargs)
        self.assertIn("components", kwargs)

    def test_closed_dms_raise_dmclosed(self):
        user = MagicMock()
        user.send = AsyncMock(side_effect=Exception("403 Forbidden: Cannot send"))
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)

        with self.assertRaises(_outbox.DMClosed):
            asyncio.run(_outbox._send_dm(bot, MagicMock(), _row()))

    def test_unknown_user_raises_dmclosed(self):
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=None)
        with self.assertRaises(_outbox.DMClosed):
            asyncio.run(_outbox._send_dm(bot, MagicMock(), _row()))

    def test_empty_payload_is_a_real_error(self):
        """Nothing to say is a bug in the enqueuer, not a delivery problem —
        it must NOT be swallowed as a closed DM."""
        user = MagicMock()
        user.send = AsyncMock()
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)
        row = _row(embed_json=None, components_json=None, content=None)
        with self.assertRaises(RuntimeError):
            asyncio.run(_outbox._send_dm(bot, MagicMock(), row))


class TestDrainDMBranch(unittest.TestCase):
    """The drain's own bookkeeping for dm rows."""

    def setUp(self):
        self._saved = sys.modules.get("interactions")
        _install_interactions()

    def tearDown(self):
        if self._saved is not None:
            sys.modules["interactions"] = self._saved

    def _drain_with(self, send_side_effect):
        # The drain's stale-row reclaim compares `DiscordOutbox.created_at` to
        # a datetime. conftest's db.models stub makes that a MagicMock, and
        # `MagicMock < datetime` raises inside the drain's own try/except —
        # swallowing the whole batch. Give it comparison-safe columns.
        class _Col:
            def __lt__(self, other):
                return True

            def __eq__(self, other):
                return True

            def asc(self):
                return self

        class _Outbox:
            created_at = _Col()
            status = _Col()

        self._saved_model = _outbox.DiscordOutbox
        _outbox.DiscordOutbox = _Outbox
        self.addCleanup(setattr, _outbox, "DiscordOutbox", self._saved_model)

        row = _row()
        row.status = "pending"
        session = MagicMock()
        query = session.query.return_value
        query.filter.return_value.update.return_value = 0
        (
            query.filter.return_value.order_by.return_value.limit.return_value
            .with_for_update.return_value.all.return_value
        ) = [row]

        user = MagicMock()
        user.send = AsyncMock(side_effect=send_side_effect)
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)

        sent = asyncio.run(_outbox.drain_once(bot, lambda: session, limit=5))
        return row, sent

    def test_successful_dm_marks_sent(self):
        row, sent = self._drain_with(None)
        self.assertEqual(sent, 1)
        self.assertEqual(row.status, "sent")

    def test_bounced_dm_marks_sent_with_reason_not_failed(self):
        """The property that stops a blocked recipient generating an identical
        failure row on every drain, forever."""
        row, sent = self._drain_with(Exception("403 Forbidden"))
        self.assertEqual(sent, 1)
        self.assertEqual(row.status, "sent")
        self.assertIn("dm not delivered", row.error)
        self.assertIn("403", row.error)


if __name__ == "__main__":
    unittest.main()
