"""Unit tests for forum-post syndication (suggestions & bug reports).

Covers the ``kind='forum_post'`` branch of services/discord_outbox.py and the
Discord content builder in web_api/routes/suggestions.py. Discord itself is
mocked; these tests assert the thread name, tag lookup, mention safety, and
status write-back rules.
"""
import asyncio
import importlib.util
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

# `services` is not a package; load the module from its file path (same
# pattern as test_event_notifications.py). The conftest db.models stub
# satisfies its top-level `from db.models import ...`.
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "services", "discord_outbox.py",
)
_spec = importlib.util.spec_from_file_location("_discord_outbox_under_test", _MODULE_PATH)
_outbox = importlib.util.module_from_spec(_spec)
sys.modules["_discord_outbox_under_test"] = _outbox
_spec.loader.exec_module(_outbox)
_create_forum_post = _outbox._create_forum_post
_mark_ref_failed = _outbox._mark_ref_failed

from web_api.routes.suggestions import _discord_content, _discord_reply_content  # noqa: E402


def _suggestion(**kw):
    sug = MagicMock()
    sug.id = kw.get("id", 7)
    sug.type = kw.get("type", "bug")
    sug.title = kw.get("title", "Lootboard skips seasonal drops")
    sug.status = "pending"
    sug.discord_thread_id = None
    return sug


def _session_returning(sug):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = sug
    return session


def _outbox_row(**kw):
    row = MagicMock()
    row.kind = kw.get("kind", "forum_post")
    row.channel_id = kw.get("channel_id", "1524869345627340871")
    row.content = kw.get("content", "Something broke\n\n-# footer")
    row.ref_type = kw.get("ref_type", "suggestion")
    row.ref_id = kw.get("ref_id", 7)
    return row


def _forum_channel(tag=None):
    channel = MagicMock()
    channel.get_tag = MagicMock(return_value=tag)
    channel.create_post = AsyncMock(return_value=MagicMock(id=999888777))
    return channel


class TestCreateForumPost(unittest.TestCase):
    def test_posts_and_writes_back_thread_id(self):
        sug = _suggestion(type="bug")
        channel = _forum_channel()
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)
        row = _outbox_row()

        asyncio.run(_create_forum_post(bot, _session_returning(sug), row))

        name, content = channel.create_post.call_args.args
        self.assertEqual(name, "\N{BUG} Lootboard skips seasonal drops")
        self.assertEqual(content, row.content)
        self.assertEqual(row.discord_message_id, "999888777")
        self.assertEqual(sug.discord_thread_id, "999888777")
        self.assertEqual(sug.status, "posted")

    def test_suggestion_type_gets_bulb_prefix(self):
        sug = _suggestion(type="suggestion", title="Dark theme")
        channel = _forum_channel()
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)

        asyncio.run(_create_forum_post(bot, _session_returning(sug), _outbox_row()))

        name, _ = channel.create_post.call_args.args
        self.assertEqual(name, "\N{ELECTRIC LIGHT BULB} Dark theme")

    def test_matching_forum_tag_is_applied(self):
        tag = MagicMock()
        channel = _forum_channel(tag=tag)
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)

        asyncio.run(_create_forum_post(bot, _session_returning(_suggestion()), _outbox_row()))

        self.assertEqual(channel.create_post.call_args.kwargs.get("applied_tags"), [tag])

    def test_no_tags_defined_is_fine(self):
        channel = _forum_channel(tag=None)
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)

        asyncio.run(_create_forum_post(bot, _session_returning(_suggestion()), _outbox_row()))

        self.assertNotIn("applied_tags", channel.create_post.call_args.kwargs)

    def test_author_ping_produces_allowed_mentions(self):
        # interactions is stubbed in unit tests; assert the boundary behavior:
        # content with a user mention sends an explicit allowed_mentions, and
        # mention-free content sends none (Discord's parse-everything default
        # is never widened by the footer).
        channel = _forum_channel()
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)
        row = _outbox_row(content="body\n\n-# submitted by <@123456789>")

        asyncio.run(_create_forum_post(bot, _session_returning(_suggestion()), row))
        self.assertIn("allowed_mentions", channel.create_post.call_args.kwargs)

        channel2 = _forum_channel()
        bot.fetch_channel = AsyncMock(return_value=channel2)
        row2 = _outbox_row(content="no pings here")
        asyncio.run(_create_forum_post(bot, _session_returning(_suggestion()), row2))
        self.assertNotIn("allowed_mentions", channel2.create_post.call_args.kwargs)

    def test_non_forum_channel_raises(self):
        channel = MagicMock(spec=[])  # no create_post attribute
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)

        with self.assertRaises(RuntimeError):
            asyncio.run(_create_forum_post(bot, _session_returning(_suggestion()), _outbox_row()))

    def test_thread_name_capped_at_100(self):
        sug = _suggestion(title="x" * 100)
        channel = _forum_channel()
        bot = MagicMock()
        bot.fetch_channel = AsyncMock(return_value=channel)

        asyncio.run(_create_forum_post(bot, _session_returning(sug), _outbox_row()))

        name, _ = channel.create_post.call_args.args
        self.assertEqual(len(name), 100)


class TestMarkRefFailed(unittest.TestCase):
    def test_flips_suggestion_to_failed(self):
        sug = _suggestion()
        _mark_ref_failed(_session_returning(sug), _outbox_row())
        self.assertEqual(sug.status, "failed")

    def test_ignores_non_forum_rows(self):
        sug = _suggestion()
        _mark_ref_failed(_session_returning(sug), _outbox_row(kind="message"))
        self.assertEqual(sug.status, "pending")


class TestDiscordContent(unittest.TestCase):
    def test_footer_pings_author_and_links_site(self):
        content = _discord_content("bug", "It broke.", "123", 42)
        self.assertIn("It broke.", content)
        self.assertIn("<@123>", content)
        self.assertIn("Bug report `#42`", content)
        self.assertIn("droptracker.io/suggestions", content)

    def test_no_discord_id_falls_back_gracefully(self):
        content = _discord_content("suggestion", "An idea.", None, 1)
        self.assertNotIn("<@", content)
        self.assertIn("a DropTracker user", content)
        self.assertIn("Suggestion `#1`", content)

    def test_long_body_truncated_under_message_limit(self):
        content = _discord_content("bug", "y" * 4000, "123", 42)
        self.assertLessEqual(len(content), 2000)
        self.assertIn("truncated", content)
        # The attribution footer must survive truncation.
        self.assertIn("<@123>", content)


class TestDiscordReplyContent(unittest.TestCase):
    def test_reply_carries_body_and_attribution(self):
        content = _discord_reply_content("I can reproduce this.", "456", "zezima")
        self.assertIn("I can reproduce this.", content)
        self.assertIn("<@456>", content)
        self.assertIn("droptracker.io", content)

    def test_reply_without_discord_id_uses_name(self):
        content = _discord_reply_content("Same here.", None, "zezima")
        self.assertNotIn("<@", content)
        self.assertIn("**zezima**", content)

    def test_reply_stays_under_message_limit(self):
        content = _discord_reply_content("z" * 5000, "456", "zezima")
        self.assertLessEqual(len(content), 2000)


if __name__ == "__main__":
    unittest.main()
